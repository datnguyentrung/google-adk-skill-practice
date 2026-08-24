# Ingestion benchmark fixtures

The benchmark has two independent comparisons so the skill rewrite is not
confounded with the contract migration.

1. End-to-end regression compares the fixed legacy stack at commit
   `ee714f97e9cf4259c19fd58130e2d17ad2cd1ff3` with the working-tree stack.
   Run the legacy side in an isolated temporary worktree/process.
2. Skill-only ablation uses working-tree code for both configurations and
   compares `minimally_migrated` with the rewritten production skill.

Run each of the three evals three times for each configuration, with the same
model ID, prompt, artifact, and recording persistence adapter. Never point the
benchmark at a real Neo4j database. Run the full matrix with:

```text
.venv/Scripts/python tests/fixtures/ingestion/evals/run_matrix.py --model <model-id>
```

The runner creates the isolated legacy worktree, uses ADK in-memory artifact
and session services, replaces Neo4j with a recording adapter, grades objective
assertions, aggregates both layers, and generates a static `review.html`.

Write transcripts, timing, metrics, and grading only below
`.eval/ingestion/workspaces/`. To regenerate reports manually, use:

```text
python .agents/skills/skill-creator/scripts/aggregate_benchmark.py <workspace>
python .agents/skills/skill-creator/eval-viewer/generate_review.py <workspace>
```

The generated workspace is intentionally Git-ignored. The eval definitions and
both skill baselines are tracked fixtures.
