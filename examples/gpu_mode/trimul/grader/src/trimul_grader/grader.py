"""GPU Mode Triton kernel grader - local MPS mode."""

from __future__ import annotations

import os
import traceback

os.environ.setdefault("GPUMODE_USE_MODAL", "true")
os.environ.setdefault("GPUMODE_MODAL_GPU", "H100")

from coral.grader import TaskGrader
from coral.types import ScoreBundle

from . import shared_eval


class Grader(TaskGrader):
    def evaluate(self) -> ScoreBundle:
        program_file = self.args.get("program_file", "initial_program.py")
        program_path = os.path.join(self.codebase_path, program_file)

        if not os.path.exists(program_path):
            return self.fail(f"Program file not found: {program_file}")

        try:
            result = shared_eval.evaluate(program_path)
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
