#!/usr/bin/env python3
"""Run both 3x3x2 benchmark layers and build skill-creator review artifacts."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


FIXED_POINT = "ee714f97e9cf4259c19fd58130e2d17ad2cd1ff3"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".eval/ingestion/workspaces/latest"),
    )
    parser.add_argument("--runs", type=int, default=3)
    return parser.parse_args()


def run_layer(
    name: str,
    configurations: list[tuple[str, Path, Path]],
    args: argparse.Namespace,
    root: Path,
) -> None:
    case_runner = Path(__file__).resolve().with_name("run_case.py")
    eval_file = root / "tests/fixtures/ingestion/evals/evals.json"
    workspace = args.workspace.resolve() / name
    for eval_id in (1, 2, 3):
        for configuration, repo, skill in configurations:
            for run_number in range(1, args.runs + 1):
                subprocess.run(
                    [
                        sys.executable,
                        str(case_runner),
                        "--repo-root",
                        str(repo),
                        "--skill-dir",
                        str(skill),
                        "--eval-file",
                        str(eval_file),
                        "--eval-id",
                        str(eval_id),
                        "--run-number",
                        str(run_number),
                        "--configuration",
                        configuration,
                        "--workspace",
                        str(workspace),
                        "--model",
                        args.model,
                        "--env-file",
                        str(root / ".env"),
                    ],
                    check=True,
                    cwd=repo,
                )

    aggregate = root / ".agents/skills/skill-creator/scripts/aggregate_benchmark.py"
    viewer = root / ".agents/skills/skill-creator/eval-viewer/generate_review.py"
    subprocess.run([sys.executable, str(aggregate), str(workspace)], check=True)
    subprocess.run(
        [
            sys.executable,
            str(viewer),
            str(workspace),
            "--benchmark",
            str(workspace / "benchmark.json"),
            "--static",
            str(workspace / "review.html"),
            "--skill-name",
            f"ingestion-{name}",
        ],
        check=True,
    )


def main() -> None:
    args = arguments()
    root = args.repo_root.resolve()
    rewritten = root / "app/skills/ingestion"
    minimal = root / "tests/fixtures/ingestion/skills/minimally_migrated"
    with tempfile.TemporaryDirectory(prefix="ingestion-legacy-") as temp_dir:
        legacy = Path(temp_dir) / "repo"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(legacy), FIXED_POINT],
            check=True,
            cwd=root,
        )
        try:
            run_layer(
                "end_to_end_regression",
                [
                    ("without_skill", legacy, legacy / "app/skills/ingestion"),
                    ("with_skill", root, rewritten),
                ],
                args,
                root,
            )
            run_layer(
                "skill_only_ablation",
                [
                    ("without_skill", root, minimal),
                    ("with_skill", root, rewritten),
                ],
                args,
                root,
            )
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(legacy)],
                check=False,
                cwd=root,
            )


if __name__ == "__main__":
    main()
