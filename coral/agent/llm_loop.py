"""In-process LLM + tool-use agent loop (the "llm_agent" runtime worker).

This is CORAL's answer to a tool-calling LLM agent, as opposed to an external
CLI coding agent (claude/codex/opencode). It is a faithful port of the *worker*
in the `aes` (Agentic Evolutionary Search) project: a stateless-per-iteration
LLM that edits code through exactly two tools — ``read_file`` and ``write_file``
— and never runs the evaluation itself. This harness runs the evaluation
(``coral eval`` semantics via :func:`coral.hooks.post_commit.submit_eval`) after
each edit turn and feeds the score/feedback back as the next round's context,
playing the role that ``aes``'s advisor + harness play.

Design notes:
- **No shell, no web search.** The agent's entire capability is read_file /
  write_file, matching aes's worker tool surface so CORAL can serve as an
  apples-to-apples benchmark for aes-style agents.
- **Stateless worker, persistent memory.** Each iteration starts from a fresh
  message history; continuity comes from CORAL's shared state (the attempts
  leaderboard), which this harness reads to rebuild context. That makes the
  process safe to kill and restart at any point (no session resume needed).
- **Cost tracking is automatic.** Model calls go through the OpenAI-compatible
  CORAL gateway (LiteLLM), so usage lands in ``.coral/public/gateway`` and shows
  up in ``coral cost`` exactly like the CLI runtimes.

Run as: ``python -m coral.agent.llm_loop --worktree ... --agent-id ... --model ...``
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --- Tool definitions (OpenAI function-calling schema) ----------------------
# Mirrors aes WORKER_TOOLS (aes/evolve.py:365-401): read any file, write files.

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read any file in the workspace by path "
                "(relative to the workspace root, or absolute)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file to read.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file, replacing it entirely. Creates parent "
                "directories as needed. Use this to edit the solution."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path of the file to write (relative to workspace root, or absolute).",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete new content for the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
]


# --- Structured logging -----------------------------------------------------
# The manager does not parse these lines; it only relies on the log file's
# mtime staying fresh (stall detection). We emit JSON lines so the web UI can
# render the trace and so runs are debuggable.

_log_lock = threading.Lock()


def emit(obj: dict[str, Any]) -> None:
    """Write one JSON log line to stdout (redirected to the agent log file)."""
    obj.setdefault("timestamp", datetime.now(UTC).isoformat())
    with _log_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


# --- Tool dispatch ----------------------------------------------------------

_MAX_TOOL_RESULT_CHARS = 100_000


def _resolve(worktree: Path, path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else worktree / p


def dispatch_tool(name: str, args: dict[str, Any], worktree: Path) -> str:
    """Execute a tool call. Free writes to any path (per CORAL config decision)."""
    if name == "read_file":
        path = _resolve(worktree, str(args.get("path", "")))
        try:
            text = path.read_text(errors="replace")
        except Exception as exc:  # noqa: BLE001 - report back to the model
            return f"Error reading {path}: {exc}"
        if len(text) > _MAX_TOOL_RESULT_CHARS:
            return text[:_MAX_TOOL_RESULT_CHARS] + "\n\n[truncated]"
        return text

    if name == "write_file":
        rel = str(args.get("path", ""))
        content = args.get("content", "")
        if not rel:
            return "Error: 'path' is required."
        try:
            target = _resolve(worktree, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        except Exception as exc:  # noqa: BLE001
            return f"Error writing {rel}: {exc}"
        return f"{rel} written successfully ({len(content)} chars)."

    return f"Unknown tool: {name}"


# --- Model client (via the OpenAI-compatible CORAL gateway) -----------------


def make_client(gateway_url: str | None, gateway_key: str | None) -> Any:
    """Build an OpenAI client pointed at the CORAL gateway when available.

    The gateway (LiteLLM proxy) exposes an OpenAI-compatible endpoint that
    speaks for every provider, so a single OpenAI client handles both
    Anthropic- and OpenAI-backed models addressed by their configured
    ``model_name``. Without a gateway, fall back to the standard OpenAI
    environment (useful only for OpenAI models / local testing).
    """
    from openai import OpenAI

    if gateway_url:
        base = gateway_url.rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"
        return OpenAI(base_url=base, api_key=gateway_key or "sk-coral-gateway")
    return OpenAI()


def run_agent_turn(
    client: Any,
    model: str,
    system: str,
    history: list[dict[str, Any]],
    worktree: Path,
    max_tokens: int,
    max_tool_iters: int,
    iteration: int,
) -> str:
    """Run one edit turn to completion with a tool-calling loop.

    The turn ends when the model stops requesting tools (or hits the tool-call
    cap / a length stop). Returns the model's final free-text summary, which
    becomes the eval commit message. Appends to ``history`` in place.
    """
    for _ in range(max_tool_iters):
        messages = [{"role": "system", "content": system}, *history]
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            max_tokens=max_tokens,
            timeout=600,
        )
        choice = response.choices[0]
        msg = choice.message
        tool_calls = msg.tool_calls or []

        # Record the assistant message in OpenAI wire format. content is left as
        # the SDK returned it (None when the turn is purely tool calls), which is
        # the form providers expect alongside tool_calls.
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
        history.append(assistant_msg)

        emit(
            {
                "type": "assistant",
                "iteration": iteration,
                "text": (msg.content or "")[:2000],
                "tool_calls": [
                    {"name": tc.function.name, "arguments": tc.function.arguments[:500]}
                    for tc in tool_calls
                ],
                "finish_reason": choice.finish_reason,
            }
        )

        if not tool_calls:
            return (msg.content or "").strip()

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                result = f"Error: invalid JSON arguments for {tc.function.name}."
            else:
                result = dispatch_tool(tc.function.name, args, worktree)
            history.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            emit(
                {
                    "type": "tool_result",
                    "iteration": iteration,
                    "name": tc.function.name,
                    "preview": result[:1000],
                }
            )

    emit({"type": "warning", "iteration": iteration, "message": "hit max_tool_iters; evaluating current state"})
    return ""


# --- Context: task info + prior experiments (the "advisor" state) -----------


def _read_coral_dir(worktree: Path) -> Path:
    breadcrumb = worktree / ".coral_dir"
    if breadcrumb.exists():
        return Path(breadcrumb.read_text().strip())
    # Fall back to the search helper used elsewhere in CORAL.
    from coral.cli._helpers import find_coral_dir

    found = find_coral_dir(worktree)
    if found is None:
        raise FileNotFoundError(f"No .coral dir resolvable from {worktree}")
    return Path(found)


def load_prior_experiments(coral_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    """Rebuild this agent's evaluated history from the shared attempts store.

    Continuity lives here (not in conversation history), so a restarted process
    resumes coherently. Newest last.
    """
    attempts_dir = coral_dir / "public" / "attempts"
    if not attempts_dir.exists():
        return []
    records = []
    for f in attempts_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("agent_id") != agent_id or data.get("status") == "pending":
            continue
        records.append(data)
    records.sort(key=lambda d: d.get("timestamp", ""))
    return records


def _best_of(experiments: list[dict[str, Any]], direction: str) -> float | None:
    scores = [e["score"] for e in experiments if isinstance(e.get("score"), (int, float))]
    if not scores:
        return None
    return min(scores) if direction == "minimize" else max(scores)


def build_system_prompt(task_name: str, task_description: str, direction: str) -> str:
    better = "lower is better" if direction == "minimize" else "higher is better"
    return (
        "You are an expert research engineer iteratively improving a solution.\n\n"
        f"# Task: {task_name}\n\n{task_description}\n\n"
        f"The objective is a numeric score where **{better}**.\n\n"
        "## How you work\n"
        "You edit the codebase using two tools: `read_file` (read any file) and "
        "`write_file` (overwrite a file with new content). You CANNOT run shell "
        "commands, execute code, or search the web. After you finish editing, an "
        "automated harness evaluates the current state of the codebase in a separate "
        "process and reports the score and feedback back to you on the next round.\n\n"
        "## Guidelines\n"
        "- Inspect the relevant files with `read_file` before changing them.\n"
        "- Make one focused, promising improvement per round, then end your turn so "
        "it can be evaluated. A rough change that scores beats a perfect change that "
        "is never evaluated.\n"
        "- Keep the code runnable — the grader runs it independently.\n"
        "- End your turn with a one-sentence summary of what you changed and why; it "
        "becomes the label for this evaluation."
    )


def build_iteration_prompt(
    iteration: int,
    experiments: list[dict[str, Any]],
    best: float | None,
    extra: str | None,
) -> str:
    lines: list[str] = []
    if iteration == 1 and not experiments:
        lines.append(
            "This is your first attempt. Explore the codebase to understand the "
            "current solution, then make your first improvement."
        )
    else:
        if best is not None:
            lines.append(f"Best score so far: {best}.")
        recent = experiments[-5:]
        if recent:
            lines.append("\nRecent evaluations (oldest first):")
            for e in recent:
                score = e.get("score")
                score_s = "n/a" if score is None else f"{score}"
                lines.append(f"- [{e.get('status', '?')}] score={score_s}: {e.get('title', '')[:120]}")
                fb = (e.get("feedback") or "").strip()
                if fb:
                    lines.append(f"    feedback: {fb[:600]}")
        lines.append(
            "\nBuild on what worked and address the feedback. Make your next "
            "improvement now."
        )
    if extra:
        lines.append(f"\nAdditional guidance:\n{extra}")
    return "\n".join(lines)


# --- Evaluation (harness-driven, aes-style) ---------------------------------


def evaluate(coral_dir: Path, agent_id: str, worktree: Path, message: str) -> dict[str, Any] | None:
    """Commit the agent's edits and run the grader, polling to keep the log fresh.

    Returns the finalized attempt dict, or None if there was nothing to commit.
    """
    from coral.hooks.post_commit import submit_eval

    label = (message.splitlines()[0] if message else "").strip()[:200] or "llm_agent iteration"
    try:
        pending = submit_eval(message=label, agent_id=agent_id, workdir=str(worktree), wait=False)
    except RuntimeError as exc:
        # Typically "nothing staged" — the model made no file changes this turn.
        emit({"type": "eval_skipped", "reason": str(exc)})
        return None

    attempt_file = coral_dir / "public" / "attempts" / f"{pending.commit_hash}.json"
    emit({"type": "eval_submitted", "commit": pending.commit_hash, "title": label})

    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        time.sleep(2.0)
        try:
            data = json.loads(attempt_file.read_text())
        except (json.JSONDecodeError, OSError):
            emit({"type": "eval_waiting", "commit": pending.commit_hash})
            continue
        if data.get("status") and data.get("status") != "pending":
            emit(
                {
                    "type": "eval_result",
                    "commit": pending.commit_hash,
                    "status": data.get("status"),
                    "score": data.get("score"),
                    "feedback": (data.get("feedback") or "")[:1000],
                }
            )
            return data
        emit({"type": "eval_waiting", "commit": pending.commit_hash})
    emit({"type": "eval_timeout", "commit": pending.commit_hash})
    return None


# --- Main loop --------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    from coral.config import CoralConfig

    worktree = Path(args.worktree).resolve()
    coral_dir = _read_coral_dir(worktree)
    config = CoralConfig.from_yaml(coral_dir / "config.yaml")
    direction = config.grader.direction

    system = build_system_prompt(config.task.name, config.task.description, direction)
    client = make_client(args.gateway_url or None, args.gateway_key or None)

    experiments = load_prior_experiments(coral_dir, args.agent_id)
    best = _best_of(experiments, direction)

    emit(
        {
            "type": "coral",
            "subtype": "start",
            "agent_id": args.agent_id,
            "model": args.model,
            "prior_experiments": len(experiments),
            "best": best,
        }
    )

    extra = args.prompt
    if extra in (None, "", "Begin.", "Session resumed. Continue where you left off."):
        extra = None

    for iteration in range(len(experiments) + 1, len(experiments) + args.max_iterations + 1):
        user = build_iteration_prompt(iteration, experiments, best, extra)
        extra = None  # only inject the manager's prompt on the first round
        emit({"type": "coral", "subtype": "prompt", "iteration": iteration, "prompt": user})

        history: list[dict[str, Any]] = [{"role": "user", "content": user}]
        try:
            summary = run_agent_turn(
                client, args.model, system, history, worktree,
                args.max_tokens, args.max_tool_iters, iteration,
            )
        except Exception as exc:  # noqa: BLE001 - keep the loop alive across API blips
            emit({"type": "error", "iteration": iteration, "message": f"{type(exc).__name__}: {exc}"})
            time.sleep(15)
            continue

        result = evaluate(coral_dir, args.agent_id, worktree, summary or f"iteration {iteration}")
        if result is not None:
            experiments.append(result)
            best = _best_of(experiments, direction)

    emit({"type": "coral", "subtype": "done", "iterations": args.max_iterations, "best": best})
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="coral.agent.llm_loop")
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-iterations", type=int, default=200)
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--max-tool-iters", type=int, default=60)
    parser.add_argument("--prompt", default=None)
    # Gateway credentials come from the environment (set by the runtime) so the
    # secret key never appears in the process argument list.
    parser.add_argument("--gateway-url", default=os.environ.get("CORAL_GATEWAY_URL") or None)
    parser.add_argument("--gateway-key", default=os.environ.get("CORAL_GATEWAY_KEY") or None)
    args = parser.parse_args()
    try:
        sys.exit(run(args))
    except KeyboardInterrupt:
        emit({"type": "coral", "subtype": "interrupted"})
        sys.exit(0)


if __name__ == "__main__":
    main()
