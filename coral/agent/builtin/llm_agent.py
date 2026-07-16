"""LLM + tool-use agent runtime.

Unlike the CLI runtimes (claude/codex/opencode), which drive an external coding
agent, this runtime runs CORAL's own in-process tool-calling loop
(:mod:`coral.agent.llm_loop`) as a subprocess. The loop is a faithful port of
the `aes` worker: an LLM with exactly two tools (``read_file`` / ``write_file``)
whose edits are evaluated by the harness rather than by the agent itself. See
that module for the design rationale.

The subprocess wrapper is deliberate: it lets the loop satisfy CORAL's existing
``AgentHandle`` contract (pid / process group / SIGINT / stall-mtime) verbatim,
so nothing in the orchestration layer needs to change.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from coral.agent.runtime import AgentHandle, write_coral_log_entry
from coral.workspace.repo import _clean_env

logger = logging.getLogger(__name__)


class LLMAgentRuntime:
    """Spawn and manage CORAL's in-process LLM tool-use agent loop."""

    @property
    def instruction_filename(self) -> str:
        # The manager still writes this file; the loop builds its own worker
        # system prompt from the task config, so the contents are not consumed
        # by the agent. Keep the CORAL.md name for consistency/inspection.
        return "CORAL.md"

    @property
    def shared_dir_name(self) -> str:
        return ".llm_agent"

    def extract_session_id(self, log_path: Path) -> str | None:
        # Session resume is intentionally not supported yet — continuity comes
        # from the shared attempts store, which the loop reloads on startup.
        return None

    def start(
        self,
        worktree_path: Path,
        coral_md_path: Path,
        model: str = "claude-sonnet-4-6",
        runtime_options: dict[str, Any] | None = None,
        max_turns: int = 200,
        log_dir: Path | None = None,
        verbose: bool = False,
        resume_session_id: str | None = None,
        prompt: str | None = None,
        prompt_source: str | None = None,
        task_name: str | None = None,
        task_description: str | None = None,
        gateway_url: str | None = None,
        gateway_api_key: str | None = None,
    ) -> AgentHandle:
        """Start the LLM tool-use loop in the given worktree."""
        agent_id_file = worktree_path / ".coral_agent_id"
        agent_id = agent_id_file.read_text().strip() if agent_id_file.exists() else "unknown"

        if log_dir is None:
            log_dir = worktree_path / ".llm_agent" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        log_idx = len(list(log_dir.glob(f"{agent_id}*.log")))
        log_path = log_dir / f"{agent_id}.{log_idx}.log"

        opts = runtime_options or {}
        cmd = [
            sys.executable,
            "-m",
            "coral.agent.llm_loop",
            "--worktree", str(worktree_path),
            "--agent-id", agent_id,
            "--model", model,
            "--max-iterations", str(max_turns),
            "--max-tokens", str(int(opts.get("max_tokens", 16384))),
            "--max-tool-iters", str(int(opts.get("max_tool_iters", 60))),
        ]
        # Only forward a substantive prompt (heartbeat/restart feedback); the
        # loop ignores the trivial default openers on its own as a safeguard.
        if prompt:
            cmd.extend(["--prompt", prompt])

        logger.info(f"Starting LLM agent {agent_id} in {worktree_path}")
        logger.info(f"Command: {' '.join(cmd[:8])} ...")

        # Invoke via the manager's own interpreter (sys.executable) so `coral`
        # and `openai` resolve regardless of the worktree venv. The loop only
        # edits files and calls the grader; it does not run the task code.
        agent_env = _clean_env()
        if gateway_url:
            agent_env["CORAL_GATEWAY_URL"] = gateway_url
            logger.info(f"LLM agent {agent_id}: routing via gateway at {gateway_url}")
        if gateway_api_key:
            agent_env["CORAL_GATEWAY_KEY"] = gateway_api_key

        log_file = open(log_path, "w", buffering=1)

        write_coral_log_entry(
            log_file,
            prompt=prompt or "Begin.",
            source=prompt_source or "start",
            agent_id=agent_id,
            session_id=None,
            task_name=task_name,
            task_description=task_description,
        )

        if verbose:
            process = subprocess.Popen(
                cmd,
                cwd=str(worktree_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=agent_env,
            )

            def _tee_output(proc: subprocess.Popen, log_f: Any, agent: str) -> None:
                try:
                    assert proc.stdout is not None
                    for line in iter(proc.stdout.readline, b""):
                        decoded = line.decode("utf-8", errors="replace")
                        sys.stdout.write(f"[{agent}] {decoded}")
                        sys.stdout.flush()
                        log_f.write(decoded)
                        log_f.flush()
                except Exception as e:  # noqa: BLE001
                    logger.error(f"Tee thread error: {e}")
                finally:
                    log_f.close()
                    if proc.stdout:
                        try:
                            proc.stdout.close()
                        except Exception:
                            pass

            tee_thread = threading.Thread(
                target=_tee_output, args=(process, log_file, agent_id), daemon=True,
            )
            tee_thread.start()
            log_file_ref = None
        else:
            process = subprocess.Popen(
                cmd,
                cwd=str(worktree_path),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=agent_env,
            )
            log_file_ref = log_file

        logger.info(f"LLM agent {agent_id} started with PID {process.pid}")

        return AgentHandle(
            agent_id=agent_id,
            process=process,
            worktree_path=worktree_path,
            log_path=log_path,
            session_id=None,
            _log_file=log_file_ref,
        )
