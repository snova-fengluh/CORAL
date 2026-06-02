"""Command: cost — offline token-usage and cost report for a run."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coral.cli._helpers import find_coral_dir

# Default per-million-token prices in USD. Override with --pricing PATH.
# Keys match the `model` field as logged by the gateway. Substring fallback
# is also tried (e.g. a logged "claude-sonnet-4-6-20250101" matches
# "claude-sonnet-4-6").
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
    },
    "claude-opus-4-6": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
    },
    "claude-opus-4-5": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
    },
    "claude-opus-4": {
        "input": 15.0,
        "output": 75.0,
        "cache_read": 1.5,
        "cache_write": 18.75,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    },
    "claude-sonnet-4-5": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    },
    "claude-sonnet-4": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_read": 0.1,
        "cache_write": 1.25,
    },
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.requests += other.requests

    def cost(self, prices: dict[str, float] | None) -> float | None:
        if prices is None:
            return None
        return (
            self.input_tokens * prices.get("input", 0.0)
            + self.output_tokens * prices.get("output", 0.0)
            + self.cache_read_tokens * prices.get("cache_read", 0.0)
            + self.cache_write_tokens * prices.get("cache_write", 0.0)
        ) / 1_000_000.0


@dataclass
class ModelStats:
    usage: Usage = field(default_factory=Usage)
    by_agent: dict[str, Usage] = field(default_factory=lambda: defaultdict(Usage))


def _extract_usage(entry: dict[str, Any]) -> Usage | None:
    """Extract a Usage record from a single gateway log entry.

    Handles three response shapes the gateway may store:
      1. Parsed dict with a top-level `usage` (non-streaming JSON).
      2. Assembled dict with a `usage` key (already-parsed SSE).
      3. Raw SSE string — we walk `data:` lines for `message_delta`
         (Anthropic) or a final chunk with `usage` (OpenAI).
    """
    if entry.get("status_code", 0) >= 400:
        return None

    resp = entry.get("response")
    if resp is None:
        return None

    if isinstance(resp, dict):
        usage_dict = resp.get("usage")
        if isinstance(usage_dict, dict):
            return _usage_from_dict(usage_dict)
        return None

    if isinstance(resp, str):
        return _usage_from_sse(resp)

    return None


def _usage_from_dict(u: dict[str, Any]) -> Usage:
    """Build a Usage from a parsed usage dict (Anthropic or OpenAI shape)."""
    # Anthropic shape: input_tokens, output_tokens, cache_read_input_tokens,
    #                  cache_creation_input_tokens
    # OpenAI shape:    prompt_tokens, completion_tokens (+ cache subfields)
    input_tokens = int(
        u.get("input_tokens")
        or u.get("prompt_tokens")
        or 0
    )
    output_tokens = int(
        u.get("output_tokens")
        or u.get("completion_tokens")
        or 0
    )
    cache_read = int(
        u.get("cache_read_input_tokens")
        or (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    cache_write = int(u.get("cache_creation_input_tokens") or 0)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        requests=1,
    )


def _usage_from_sse(raw: str) -> Usage | None:
    """Parse Anthropic/OpenAI SSE stream and return cumulative usage.

    For Anthropic: takes usage from the last `message_delta` chunk (it carries
    the running total) or falls back to `message_start`.

    For OpenAI: takes usage from any chunk that has a `usage` field (typically
    the final chunk when stream_options.include_usage is set, or a
    `response.completed` chunk for the Responses API).
    """
    last_usage: dict[str, Any] | None = None
    start_usage: dict[str, Any] | None = None

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]" or not payload:
            continue
        try:
            chunk = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue

        ctype = chunk.get("type")

        if ctype == "message_start":
            msg = chunk.get("message") or {}
            if isinstance(msg.get("usage"), dict):
                start_usage = msg["usage"]
        elif ctype == "message_delta":
            if isinstance(chunk.get("usage"), dict):
                last_usage = chunk["usage"]
        elif ctype == "response.completed":
            # OpenAI Responses API
            resp_obj = chunk.get("response") or {}
            if isinstance(resp_obj.get("usage"), dict):
                last_usage = resp_obj["usage"]
        elif isinstance(chunk.get("usage"), dict):
            # OpenAI Chat Completions: final chunk carries usage
            last_usage = chunk["usage"]

    chosen = last_usage or start_usage
    if not chosen:
        return None
    return _usage_from_dict(chosen)


def _load_pricing(pricing_arg: str | None) -> dict[str, dict[str, float]]:
    """Merge default pricing with an optional YAML override file."""
    pricing = {k: dict(v) for k, v in DEFAULT_PRICING.items()}
    if not pricing_arg:
        return pricing

    path = Path(pricing_arg)
    if not path.exists():
        print(f"Error: pricing file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        import yaml

        with open(path) as f:
            override = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Error: failed to parse pricing file {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(override, dict):
        print(f"Error: pricing file must be a mapping, got {type(override).__name__}", file=sys.stderr)
        sys.exit(1)

    for model, fields in override.items():
        if not isinstance(fields, dict):
            continue
        pricing.setdefault(model, {})
        for k in ("input", "output", "cache_read", "cache_write"):
            if k in fields:
                pricing[model][k] = float(fields[k])
    return pricing


def _resolve_prices(
    model: str | None, pricing: dict[str, dict[str, float]]
) -> dict[str, float] | None:
    """Pick the price row for a logged model name.

    Tries exact match, then a substring match against the configured keys.
    Returns None if nothing matches (cost will be reported as unknown).
    """
    if not model:
        return None
    if model in pricing:
        return pricing[model]
    for key, prices in pricing.items():
        if key in model or model in key:
            return prices
    return None


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_cost(c: float | None) -> str:
    if c is None:
        return "?"
    return f"${c:,.4f}"


def cmd_cost(args: argparse.Namespace) -> None:
    """Report token usage and estimated cost for a run.

    Reads .coral/public/gateway/requests.jsonl, sums input/output/cache
    tokens per model and per agent, and applies a pricing table to estimate
    USD cost. Pricing defaults cover common Claude models; override or
    extend via `--pricing path/to/prices.yaml` (mapping of model name to
    {input, output, cache_read, cache_write} per-million-token prices).

    Examples:
      coral cost
      coral cost --task circle-packing
      coral cost --pricing prices.yaml
      coral cost --by-agent
      coral cost --json
    """
    coral_dir = find_coral_dir(getattr(args, "task", None), getattr(args, "run", None))
    log_path = coral_dir / "public" / "gateway" / "requests.jsonl"

    if not log_path.exists():
        print(
            f"Error: no gateway log at {log_path}.\n"
            "Cost tracking requires the LiteLLM gateway "
            "(set agents.gateway.enabled=true in the task config).",
            file=sys.stderr,
        )
        sys.exit(1)

    pricing = _load_pricing(getattr(args, "pricing", None))

    by_model: dict[str, ModelStats] = defaultdict(ModelStats)
    total = Usage()
    skipped = 0
    parsed = 0

    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            usage = _extract_usage(entry)
            if usage is None:
                skipped += 1
                continue
            parsed += 1

            model = entry.get("model") or "unknown"
            agent = entry.get("agent_id") or "unknown"

            stats = by_model[model]
            stats.usage.add(usage)
            stats.by_agent[agent].add(usage)
            total.add(usage)

    if parsed == 0:
        print(f"No usage data in {log_path} (skipped {skipped} entries).")
        return

    if getattr(args, "json", False):
        out = {
            "run": str(coral_dir.parent),
            "log": str(log_path),
            "total": _usage_to_dict(total, _aggregate_cost(by_model, pricing)),
            "by_model": {
                m: {
                    "usage": _usage_to_dict(s.usage, s.usage.cost(_resolve_prices(m, pricing))),
                    "priced": _resolve_prices(m, pricing) is not None,
                    "by_agent": {
                        a: _usage_to_dict(u, u.cost(_resolve_prices(m, pricing)))
                        for a, u in s.by_agent.items()
                    },
                }
                for m, s in by_model.items()
            },
            "skipped_entries": skipped,
        }
        print(json.dumps(out, indent=2))
        return

    # Human-readable report
    print(f"Run:  {coral_dir.parent}")
    print(f"Log:  {log_path}")
    print(f"Requests parsed: {parsed}" + (f"  (skipped {skipped})" if skipped else ""))
    print()

    print("Per-model usage:")
    print(_format_model_table(by_model, pricing))

    if getattr(args, "by_agent", False):
        print()
        print("Per-agent usage (by model):")
        for model, stats in sorted(by_model.items()):
            prices = _resolve_prices(model, pricing)
            print(f"  {model}:")
            print(_format_agent_table(stats.by_agent, prices, indent="    "))

    print()
    total_cost = _aggregate_cost(by_model, pricing)
    print("Totals:")
    print(f"  requests       {_fmt_int(total.requests)}")
    print(f"  input          {_fmt_int(total.input_tokens)}")
    print(f"  output         {_fmt_int(total.output_tokens)}")
    print(f"  cache read     {_fmt_int(total.cache_read_tokens)}")
    print(f"  cache write    {_fmt_int(total.cache_write_tokens)}")
    print(f"  estimated cost {_fmt_cost(total_cost)}")

    unpriced = sorted(m for m in by_model if _resolve_prices(m, pricing) is None)
    if unpriced:
        print()
        print(
            "Note: no pricing for: "
            + ", ".join(unpriced)
            + ". Pass --pricing prices.yaml to add rates "
            "(per-million-token: input, output, cache_read, cache_write)."
        )


def _aggregate_cost(
    by_model: dict[str, ModelStats], pricing: dict[str, dict[str, float]]
) -> float | None:
    """Sum per-model costs. Returns None if nothing is priced."""
    total = 0.0
    any_priced = False
    for model, stats in by_model.items():
        prices = _resolve_prices(model, pricing)
        if prices is None:
            continue
        any_priced = True
        c = stats.usage.cost(prices)
        if c is not None:
            total += c
    return total if any_priced else None


def _usage_to_dict(u: Usage, cost: float | None) -> dict[str, Any]:
    return {
        "requests": u.requests,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cache_write_tokens": u.cache_write_tokens,
        "cost_usd": cost,
    }


def _format_model_table(
    by_model: dict[str, ModelStats], pricing: dict[str, dict[str, float]]
) -> str:
    rows = []
    for model in sorted(by_model):
        u = by_model[model].usage
        prices = _resolve_prices(model, pricing)
        rows.append(
            (
                model,
                _fmt_int(u.requests),
                _fmt_int(u.input_tokens),
                _fmt_int(u.output_tokens),
                _fmt_int(u.cache_read_tokens),
                _fmt_int(u.cache_write_tokens),
                _fmt_cost(u.cost(prices)),
            )
        )
    headers = ("MODEL", "REQS", "INPUT", "OUTPUT", "CACHE_R", "CACHE_W", "COST")
    return _render_table(headers, rows, indent="  ")


def _format_agent_table(
    by_agent: dict[str, Usage], prices: dict[str, float] | None, indent: str
) -> str:
    rows = []
    for agent in sorted(by_agent):
        u = by_agent[agent]
        rows.append(
            (
                agent,
                _fmt_int(u.requests),
                _fmt_int(u.input_tokens),
                _fmt_int(u.output_tokens),
                _fmt_int(u.cache_read_tokens),
                _fmt_int(u.cache_write_tokens),
                _fmt_cost(u.cost(prices)),
            )
        )
    headers = ("AGENT", "REQS", "INPUT", "OUTPUT", "CACHE_R", "CACHE_W", "COST")
    return _render_table(headers, rows, indent=indent)


def _render_table(
    headers: tuple[str, ...], rows: list[tuple[str, ...]], indent: str = ""
) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: tuple[str, ...]) -> str:
        # Left-align first column (label), right-align numeric columns
        parts = [row[0].ljust(widths[0])]
        for i in range(1, len(row)):
            parts.append(row[i].rjust(widths[i]))
        return indent + "  ".join(parts)

    out = [fmt(headers)]
    out.append(indent + "  ".join("-" * w for w in widths))
    out.extend(fmt(r) for r in rows)
    return "\n".join(out)
