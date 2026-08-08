from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[2] / "data" / "ontology"
DEFAULT_OUTPUT_PATH = BASE_DIR / "all.ontology.json"
MERGED_KEYS = ("classes", "edges", "attributes")
DEDUP_KEYS = ("iri", "technicalName", "name")


def resolve_directory(path: Path | str | None = None) -> Path:
    """Resolve and validate the ontology JSON input directory."""

    resolved = Path(path) if path else BASE_DIR
    resolved = resolved if resolved.is_absolute() else resolved.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Ontology directory not found: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Ontology input path must be a directory: {resolved}")
    return resolved


def resolve_output_path(path: Path | str | None = None) -> Path:
    """Resolve and prepare the merged ontology output path."""

    resolved = Path(path) if path else DEFAULT_OUTPUT_PATH
    resolved = resolved if resolved.is_absolute() else resolved.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def dedupe_key(item: Any) -> tuple[str, str] | None:
    """Return the first stable ontology identifier available for deduping."""

    if not isinstance(item, dict):
        return None

    for key in DEDUP_KEYS:
        value = item.get(key)
        if value:
            return key, str(value)
    return None


def append_unique(items: list[Any], item: Any, seen: set[tuple[str, str]]) -> None:
    """Append an ontology item unless a stable identifier was already seen."""

    key = dedupe_key(item)
    if key is None:
        items.append(item)
        return

    if key in seen:
        return

    seen.add(key)
    items.append(item)


def ontology_files(input_dir: Path, output_path: Path | None = None) -> list[Path]:
    """Find ontology JSON files recursively, excluding the merged output file."""

    resolved_output_path = output_path.resolve() if output_path else None
    files = []
    for file_path in sorted(input_dir.rglob("*.ontology.json")):
        if resolved_output_path and file_path.resolve() == resolved_output_path:
            continue
        files.append(file_path)
    return files


def load_all_ontologies(
    input_dir: Path | str | None = None,
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    """Load and merge every ontology JSON file under the input directory."""

    resolved_input_dir = resolve_directory(input_dir)
    resolved_output_path = resolve_output_path(output_path)
    files = ontology_files(resolved_input_dir, resolved_output_path)

    merged: dict[str, Any] = {
        "sourceDir": str(resolved_input_dir),
        "sourceFiles": [str(path) for path in files],
        "summary": {
            "sourceFiles": len(files),
            "classes": 0,
            "edges": 0,
            "attributes": 0,
        },
        "classes": [],
        "edges": [],
        "attributes": [],
    }
    seen = {key: set() for key in MERGED_KEYS}

    for file_path in files:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        for key in MERGED_KEYS:
            for item in data.get(key, []):
                append_unique(merged[key], item, seen[key])

    for key in MERGED_KEYS:
        merged["summary"][key] = len(merged[key])

    return merged


def write_merged_ontology(
    output_path: Path | str | None = None,
    input_dir: Path | str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Merge ontology JSON files and write the combined output file."""

    resolved_output_path = resolve_output_path(output_path)
    merged = load_all_ontologies(input_dir, resolved_output_path)
    resolved_output_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return resolved_output_path, merged


def print_merge_report(output_path: Path, data: dict[str, Any]) -> None:
    """Print a concise report for the merged ontology output."""

    summary = data["summary"]
    print("RDF ontology merge complete")
    print(f"Input dir: {data['sourceDir']}")
    print(f"Output file: {output_path}")
    print(f"Source files: {summary['sourceFiles']}")
    print(
        "Merged: "
        f"{summary['classes']} classes, "
        f"{summary['edges']} edges, "
        f"{summary['attributes']} attributes"
    )


def main() -> None:
    """Run the ontology JSON merge CLI."""

    parser = argparse.ArgumentParser(description="Merge ontology JSON files")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=BASE_DIR,
        help="Ontology JSON folder tree. Defaults to app/data/ontology.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Merged JSON output file. Defaults to app/data/ontology/all.ontology.json.",
    )
    args = parser.parse_args()

    output_path, data = write_merged_ontology(args.output, args.input_dir)
    print_merge_report(output_path, data)


LOAN_GENERAL = load_all_ontologies()


if __name__ == "__main__":
    main()
