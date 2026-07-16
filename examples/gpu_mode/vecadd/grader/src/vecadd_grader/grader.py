"""GPU Mode Triton kernel grader.

Thin wrapper that runs the bundled shared_eval (port of SkyDiscover's evaluator)
and maps its EvaluationResult to a CORAL ScoreBundle.

The agent's program (default: initial_program.py) must define `custom_kernel(data)`.
Correctness + benchmark logic lives in shared_eval.py (local GPU and Modal paths).

Backend selection (in priority order):
  1. ``grader.args.use_modal`` / ``grader.args.modal_gpu`` in task.yaml
  2. ``GPUMODE_USE_MODAL`` / ``GPUMODE_MODAL_GPU`` environment variables
  3. local CUDA execution

Pin Modal in task.yaml so the choice is config-driven and reaches the grader
worker subprocess reliably (env vars set on `coral eval` do NOT — grading runs
in the daemon's process, not the agent's).
"""

from __future__ import annotations

import os
import traceback

from coral.grader import TaskGrader
from coral.types import ScoreBundle

from . import shared_eval


def _as_bool(value):
    """Coerce a YAML/env value to bool, or None if unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Grader(TaskGrader):
    def evaluate(self) -> ScoreBundle:
        program_file = self.args.get("program_file", "initial_program.py")
        program_path = os.path.join(self.codebase_path, program_file)

        if not os.path.exists(program_path):
            return self.fail(f"Program file not found: {program_file}")

        use_modal = _as_bool(self.args.get("use_modal"))
        modal_gpu = self.args.get("modal_gpu")

        try:
            result = shared_eval.evaluate(program_path, use_modal=use_modal, modal_gpu=modal_gpu)
        except Exception as e:
            return self.fail(f"Evaluation crashed: {e}\n{traceback.format_exc()[-1500:]}")

        metrics = result.metrics or {}
        artifacts = result.artifacts or {}

        if "error" in artifacts:
            stage = artifacts.get("failure_stage", "error")
            tb = artifacts.get("traceback", "")
            tail = f"\n{tb[-1000:]}" if tb else ""
            return self.fail(f"[{stage}] {artifacts['error']}{tail}")

        score_val = float(metrics.get("combined_score", 0.0))

        parts = [f"score={score_val:.4f}"]
        if "geom_mean_us" in metrics:
            parts.append(f"geom_mean={metrics['geom_mean_us']:.2f}us")
        if "correctness" in metrics:
            parts.append(f"correct={metrics['correctness']:.2f}")
        hw = artifacts.get("hardware", "?")
        parts.append(f"hw={hw}")
        bench_parts = sorted(k for k in artifacts if k.startswith("bench_") and k.endswith("_mean_us"))
        if bench_parts:
            bench_str = " ".join(f"{k.replace('bench_', '').replace('_mean_us', '')}:{artifacts[k]}us" for k in bench_parts)
            parts.append(bench_str)

        return self.score(score_val, explanation=" | ".join(parts))
