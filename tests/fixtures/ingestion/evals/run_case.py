#!/usr/bin/env python3
"""Run one ADK ingestion eval with recording persistence and objective grading."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from string import Template
from typing import Any


RECORDED_WRITES: list[dict[str, Any]] = []


class RecordingFillService:
    def fill(self, patch, *args, **kwargs) -> dict[str, Any]:
        captured = (
            patch.model_dump(by_alias=True, mode="json")
            if hasattr(patch, "model_dump")
            else patch
        )
        RECORDED_WRITES.append(captured)
        nodes = captured.get("nodes", [])
        return {
            "status": "success",
            "nodes": len(nodes),
            "edges": len(captured.get("edges", [])),
            "nodeIds": {
                node.get("tempId", str(index)): f"recording-{index}"
                for index, node in enumerate(nodes)
            },
        }

    def close(self) -> None:
        pass


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    for name in ("repo-root", "skill-dir", "eval-file", "workspace"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--eval-id", type=int, required=True)
    parser.add_argument("--run-number", type=int, required=True)
    parser.add_argument("--configuration", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--env-file", type=Path)
    return parser.parse_args()


def load_case(path: Path, eval_id: int) -> dict[str, Any]:
    cases = json.loads(path.read_text(encoding="utf-8"))["evals"]
    return next(case for case in cases if case["id"] == eval_id)


def load_rendered_skill(skill_dir: Path):
    from google.adk import skills
    from google.adk.skills import models

    skill = skills.load_skill_from_dir(skill_dir)
    instructions = Template(skill.instructions).safe_substitute(
        prepare_extraction_context_tool="prepare_extraction_context",
        validate_graph_patch_tool="validate_graph_patch",
        fill_graph_patch_tool="fill_graph_patch",
    )
    return models.Skill(
        frontmatter=skill.frontmatter,
        instructions=instructions,
        resources=skill.resources,
    )


def objective_grade(
    case: dict[str, Any],
    artifact_name: str,
    calls: list[dict],
    responses: list[dict],
) -> tuple[list[dict], dict[str, float]]:
    validate = [call for call in calls if call["name"] == "validate_graph_patch"]
    fill = [call for call in calls if call["name"] == "fill_graph_patch"]
    patch = validate[-1]["args"].get("graph_patch", {}) if validate else {}
    nodes = patch.get("nodes", []) if isinstance(patch, dict) else []
    edges = patch.get("edges", []) if isinstance(patch, dict) else []
    entries = [entry for node in nodes for entry in node.get("properties", [])]
    values = {
        entry.get("propertyName"): entry.get("value")
        for entry in entries
        if isinstance(entry, dict)
    }
    oracle = case["oracle"]
    code = oracle["productCode"]
    date_value = oracle["effectiveDate"]
    statuses = {
        value for name, value in values.items() if name and name.endswith("Status")
    }
    names = [call["name"] for call in calls]
    validate_responses = [
        item["response"]
        for item in responses
        if item["name"] == "validate_graph_patch"
    ]
    validation = validate_responses[-1] if validate_responses else {}
    extraction_valid = validation.get(
        "validForExtraction", validation.get("valid", False)
    )
    persistence_valid = validation.get(
        "validForPersistence", validation.get("valid", False)
    )
    validation_issues = validation.get("errors", [])
    unknown_free = not any(
        "UNKNOWN" in str(issue).upper() or "Unknown ontology" in str(issue)
        for issue in validation_issues
    )
    facts = [*nodes, *edges]
    evidence_grounded = bool(nodes) and all(
        fact.get("evidence")
        and all(
            item.get("source") == artifact_name and item.get("text", "").strip()
            for item in fact["evidence"]
        )
        for fact in facts
    )
    rule_nodes = {
        node.get("tempId"): node for node in nodes if node.get("className") == "pskg:BusinessRule"
    }
    eligibility_targets = {
        edge.get("targetTempId")
        for edge in edges
        if edge.get("edgeName") == "pskg:hasEligibilityRule"
    }
    eligibility_ok = not oracle["requiresEligibility"] or any(
        any(
            entry.get("propertyName") == "pskg:businessRuleCondition"
            and bool(str(entry.get("value", "")).strip())
            for entry in rule_nodes[target].get("properties", [])
        )
        for target in eligibility_targets
        if target in rule_nodes
    )
    fill_indexes = [index for index, call in enumerate(calls) if call["name"] == "fill_graph_patch"]

    def fill_matches_latest_validation(fill_index: int) -> bool:
        prior_validations = [
            index
            for index, call in enumerate(calls[:fill_index])
            if call["name"] == "validate_graph_patch"
        ]
        if not prior_validations:
            return False
        validated_patch = calls[prior_validations[-1]]["args"].get("graph_patch")
        filled_patch = calls[fill_index]["args"].get("graph_patch")
        return validated_patch == filled_patch

    sequence_ok = all(
        fill_matches_latest_validation(fill_index) for fill_index in fill_indexes
    )
    should_fill = oracle["fillWhenReady"] and persistence_valid
    fill_policy_ok = bool(fill) == should_fill
    status_ok = statuses == set(oracle["allowedStatuses"])
    checks = [
        (
            "The draft uses PropertyEntry arrays",
            bool(nodes) and all(isinstance(node.get("properties"), list) for node in nodes),
        ),
        (f"Product code is {code}", values.get("pskg:productCode") == code),
        (
            f"Effective date is {date_value}",
            values.get("pskg:bankingProductEffectiveFrom") == date_value,
        ),
        ("Status emission matches the source exactly", status_ok),
        ("Every emitted fact has artifact-grounded evidence", evidence_grounded),
        ("Required eligibility condition and edge are emitted", eligibility_ok),
        ("Validation passes extraction with no unknown ontology facts", extraction_valid and unknown_free),
        ("Fill uses the latest validated draft and correct sequence", bool(validate) and sequence_ok),
        ("Fill is suppressed or executed according to readiness", fill_policy_ok),
    ]
    expectations = [
        {
            "text": text,
            "passed": passed,
            "evidence": f"tool sequence={names}; emitted values={values}",
        }
        for text, passed in checks
    ]
    quality = {
        "malformed_patch_rate": 0.0 if checks[0][1] else 1.0,
        "hallucination_rate": 0.0 if status_ok and evidence_grounded else 1.0,
        "sequencing_pass_rate": 1.0 if sequence_ok and fill_policy_ok else 0.0,
    }
    return expectations, quality


async def run(args: argparse.Namespace) -> None:
    root = args.repo_root.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file.resolve(), override=False)

    from google.adk.agents import Agent
    from google.adk.runners import InMemoryRunner
    from google.adk.skills import SkillRegistry
    from google.adk.tools.skill_toolset import SkillToolset
    from google.genai import types

    from app.tools import ingestion_tools

    skill = load_rendered_skill(args.skill_dir.resolve())

    class Registry(SkillRegistry):
        async def get_skill(self, *, name: str):
            if name != "ingestion":
                raise ValueError(name)
            return skill

        async def search_skills(self, *, query: str):
            return [skill.frontmatter]

        def search_tool_description(self) -> str:
            return "Search the benchmark skill."

    ingestion_tools.create_fill_service = lambda *a, **kw: RecordingFillService()
    toolset = SkillToolset(
        skills=[],
        registry=Registry(),
        additional_tools=ingestion_tools.get_ingestion_tools(),
    )
    agent = Agent(
        name="ingestion_benchmark_agent",
        model=args.model,
        instruction=skill.instructions,
        tools=[toolset],
    )
    runner = InMemoryRunner(agent=agent, app_name="ingestion_benchmark")
    case = load_case(args.eval_file.resolve(), args.eval_id)
    user_id = "benchmark"
    session_id = f"eval-{args.eval_id}-{args.configuration}-{args.run_number}"
    await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id=user_id,
        session_id=session_id,
        state={f"_adk_activated_skill_{agent.name}": ["ingestion"]},
    )
    artifact = root / case["files"][0]
    await runner.artifact_service.save_artifact(
        app_name=runner.app_name,
        user_id=user_id,
        session_id=session_id,
        filename=artifact.name,
        artifact=types.Part(
            inline_data=types.Blob(
                data=artifact.read_bytes(),
                display_name=artifact.name,
                mime_type="text/markdown",
            )
        ),
    )

    calls: list[dict] = []
    responses: list[dict] = []
    events: list[dict] = []
    response: list[str] = []
    total_tokens = 0
    started = time.perf_counter()
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.UserContent(
            parts=[types.Part(text=f'[Uploaded Artifact: "{artifact.name}"]\n{case["prompt"]}')]
        ),
    ):
        events.append(event.model_dump(mode="json", exclude_none=True))
        if event.usage_metadata:
            total_tokens += event.usage_metadata.total_token_count or 0
        calls.extend(
            {"name": call.name, "args": call.args or {}}
            for call in event.get_function_calls()
        )
        responses.extend(
            {"name": response.name, "response": response.response or {}}
            for response in event.get_function_responses()
        )
        if event.content:
            response.extend(part.text for part in event.content.parts or [] if part.text)
    duration = time.perf_counter() - started

    run_dir = (
        args.workspace.resolve()
        / f"eval-{args.eval_id}"
        / args.configuration
        / f"run-{args.run_number}"
    )
    outputs = run_dir / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    grading, quality = objective_grade(
        case,
        artifact.name,
        calls,
        responses,
    )
    passed = sum(item["passed"] for item in grading)
    counts = Counter(call["name"] for call in calls)
    metrics = {
        "tool_calls": dict(counts),
        "total_tool_calls": len(calls),
        "total_steps": len(events),
        "files_created": ["transcript.json", "response.txt", "recording.json"],
        "errors_encountered": 0,
        "output_chars": sum(map(len, response)),
        "transcript_chars": len(json.dumps(events, ensure_ascii=False)),
        **quality,
    }
    files = {
        outputs / "transcript.json": events,
        outputs / "recording.json": RECORDED_WRITES,
        outputs / "metrics.json": metrics,
        run_dir / "timing.json": {"total_duration_seconds": duration, "model": args.model},
        run_dir / "grading.json": {
            "expectations": grading,
            "summary": {
                "passed": passed,
                "failed": len(grading) - passed,
                "total": len(grading),
                "pass_rate": passed / len(grading),
            },
            "execution_metrics": metrics,
            "timing": {"total_duration_seconds": duration},
            "quality_metrics": quality,
        },
    }
    files[run_dir / "timing.json"]["total_tokens"] = total_tokens
    for path, value in files.items():
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    (outputs / "response.txt").write_text("\n".join(response), encoding="utf-8")
    metadata = args.workspace.resolve() / f"eval-{args.eval_id}" / "eval_metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "eval_id": args.eval_id,
                "eval_name": case["prompt"],
                "prompt": case["prompt"],
                "assertions": case["expectations"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(run(arguments()))
