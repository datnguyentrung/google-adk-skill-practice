from __future__ import annotations

import argparse
import json
import re
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


def is_generated_ontology_file(path: Path) -> bool:
    """Identify generated lowercase aggregate files."""

    return path.name.startswith("all_") or path.name == "all.ontology.json"


def folder_output_path(folder: Path) -> Path:
    """Build the generated aggregate path for one ontology folder."""

    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", folder.name)
    folder_name = "_".join(word.lower() for word in words) or folder.name.lower()
    return folder / f"all_{folder_name}.ontology.json"


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


def direct_ontology_files(
    input_dir: Path,
    output_path: Path | None = None,
) -> list[Path]:
    """Find direct ontology JSON inputs, excluding generated aggregate files."""

    resolved_output_path = output_path.resolve() if output_path else None
    files = []
    for file_path in sorted(input_dir.glob("*.ontology.json")):
        if is_generated_ontology_file(file_path):
            continue
        if resolved_output_path and file_path.resolve() == resolved_output_path:
            continue
        files.append(file_path)
    return files


def ontology_files(input_dir: Path, output_path: Path | None = None) -> list[Path]:
    """Find the input files used to build one folder's aggregate ontology."""

    files = direct_ontology_files(input_dir, output_path)
    for child_dir in sorted(path for path in input_dir.iterdir() if path.is_dir()):
        child_output_path = folder_output_path(child_dir)
        if child_output_path.exists():
            files.append(child_output_path)
    return files


def ontology_folders(root_dir: Path) -> list[Path]:
    """Find folders that contain direct or descendant ontology JSON inputs."""

    folders = [root_dir]
    folders.extend(path for path in root_dir.rglob("*") if path.is_dir())
    source_folders = {
        file_path.parent
        for file_path in root_dir.rglob("*.ontology.json")
        if not is_generated_ontology_file(file_path)
    }
    merge_folders = set(source_folders)
    for source_folder in source_folders:
        for parent in source_folder.parents:
            merge_folders.add(parent)
            if parent == root_dir:
                break
    return [
        folder
        for folder in sorted(folders)
        if folder in merge_folders and root_dir in (folder, *folder.parents)
    ]


def load_all_ontologies(
    input_dir: Path | str | None = None,
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    """Load and merge ontology JSON files directly under the input directory."""

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


def write_merged_ontology_folders(
    root_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Merge each ontology folder under a root into an in-folder output file."""

    resolved_root_dir = resolve_directory(root_dir)
    results = []
    for folder in sorted(
        ontology_folders(resolved_root_dir),
        key=lambda path: len(path.relative_to(resolved_root_dir).parts),
        reverse=True,
    ):
        output_path = (
            resolved_root_dir / DEFAULT_OUTPUT_PATH.name
            if folder == resolved_root_dir
            else folder_output_path(folder)
        )
        output_path, data = write_merged_ontology(output_path, folder)
        results.append(
            {
                "folder": str(folder),
                "output": str(output_path),
                "summary": data["summary"],
            }
        )
    return results


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


def print_folder_merge_report(results: list[dict[str, Any]]) -> None:
    """Print a concise report for folder-by-folder ontology merges."""

    print("RDF ontology folder merge complete")
    print(f"Folders merged: {len(results)}")
    for item in results:
        summary = item["summary"]
        print(
            f"- {item['output']} from {summary['sourceFiles']} files "
            f"({summary['classes']} classes, {summary['edges']} edges, "
            f"{summary['attributes']} attributes)"
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
        default=None,
        help=(
            "Merged JSON output file for single-folder mode. When omitted, "
            "each ontology folder is merged into its own all_<folder>.ontology.json."
        ),
    )
    args = parser.parse_args()

    if args.output:
        output_path, data = write_merged_ontology(args.output, args.input_dir)
        print_merge_report(output_path, data)
        return

    results = write_merged_ontology_folders(args.input_dir)
    print_folder_merge_report(results)


LOAN_GENERAL = load_all_ontologies(
    BASE_DIR / "LOAN",
    BASE_DIR / "LOAN" / "all_loan.ontology.json",
)


if __name__ == "__main__":
    main()
