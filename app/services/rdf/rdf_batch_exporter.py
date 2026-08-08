from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from app.services.rdf.rdf_inspector import inspect
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo_root))
    from app.services.rdf.rdf_inspector import inspect

# FIBO_LOAN_DIR = Path(__file__).resolve().parents[2] / "data" / "fibo" / "LOAN"
# ONTOLOGY_LOAN_DIR = Path(__file__).resolve().parents[2] / "data" / "ontology" / "LOAN"

FIBO_LOAN_DIR = Path(__file__).resolve().parents[2] / "data" / "fibo"
ONTOLOGY_LOAN_DIR = Path(__file__).resolve().parents[2] / "data" / "ontology"


def resolve_directory(path: Path | str | None, default: Path) -> Path:
    """Resolve and validate a directory path for batch export."""

    resolved = Path(path) if path else default
    resolved = resolved if resolved.is_absolute() else resolved.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Directory not found: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Path must be a directory: {resolved}")
    return resolved


def is_metadata_file(path: Path) -> bool:
    """Identify ontology metadata files that should be skipped in batch export."""

    return "metadata" in path.stem.lower()


def output_path_for(input_file: Path, input_dir: Path, output_dir: Path) -> Path:
    """Mirror an RDF file path under the batch output directory as JSON."""

    relative_path = input_file.relative_to(input_dir)
    return (output_dir / relative_path).with_suffix(".ontology.json")


def export_folder(
    input_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
    rdf_format: str | None = None,
    base: str | None = None,
    include_all: bool = False,
    lang: str = "en",
    skip_metadata: bool = True,
) -> dict[str, Any]:
    """Export every RDF file in a folder tree to mirrored ontology JSON files."""

    resolved_input_dir = resolve_directory(input_dir, FIBO_LOAN_DIR)
    resolved_output_dir = Path(output_dir) if output_dir else ONTOLOGY_LOAN_DIR
    resolved_output_dir = (
        resolved_output_dir
        if resolved_output_dir.is_absolute()
        else resolved_output_dir.resolve()
    )

    exported = []
    skipped = []
    for input_file in sorted(resolved_input_dir.rglob("*.rdf")):
        if skip_metadata and is_metadata_file(input_file):
            skipped.append(str(input_file))
            continue

        data = inspect(input_file, rdf_format, base, include_all, lang)
        output_path = output_path_for(
            input_file, resolved_input_dir, resolved_output_dir
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        exported.append(
            {
                "input": str(input_file),
                "output": str(output_path),
                "summary": data["summary"],
            }
        )

    return {
        "inputDir": str(resolved_input_dir),
        "outputDir": str(resolved_output_dir),
        "exportedCount": len(exported),
        "skippedCount": len(skipped),
        "exported": exported,
        "skipped": skipped,
    }


def print_batch_report(result: dict[str, Any]) -> None:
    """Print a concise report for a completed batch export."""

    print("RDF batch export complete")
    print(f"Input dir: {result['inputDir']}")
    print(f"Output dir: {result['outputDir']}")
    print(f"Exported: {result['exportedCount']}")
    print(f"Skipped metadata: {result['skippedCount']}")
    for item in result["exported"]:
        summary = item["summary"]
        print(
            f"- {item['output']} "
            f"({summary['nodes']} nodes, {summary['edges']} edges, "
            f"{summary['properties']} properties)"
        )


def main() -> None:
    """Run batch export for an RDF folder tree."""

    parser = argparse.ArgumentParser(description="Batch export RDF/OWL files to JSON")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=FIBO_LOAN_DIR,
        help="RDF folder tree to export. Defaults to app/data/fibo/LOAN.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ONTOLOGY_LOAN_DIR,
        help="JSON output folder. Defaults to app/data/ontology/LOAN.",
    )
    parser.add_argument(
        "--format",
        default=None,
        help="RDF parser format. When omitted, inferred per file extension.",
    )
    parser.add_argument("--base", help="Override ontology namespace/base IRI")
    parser.add_argument("--all", action="store_true", help="Include external terms")
    parser.add_argument("--lang", default="en")
    parser.add_argument(
        "--skip-metadata",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip metadata RDF files during batch export. Enabled by default.",
    )
    args = parser.parse_args()

    result = export_folder(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        rdf_format=args.format,
        base=args.base,
        include_all=args.all,
        lang=args.lang,
        skip_metadata=args.skip_metadata,
    )
    print_batch_report(result)


if __name__ == "__main__":
    main()
