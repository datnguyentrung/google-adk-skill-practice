import json
import importlib.util
from pathlib import Path

RDF_MERGE_PATH = (
    Path(__file__).parents[1] / "app" / "services" / "rdf" / "rdf_merge.py"
)
spec = importlib.util.spec_from_file_location("rdf_merge", RDF_MERGE_PATH)
rdf_merge = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(rdf_merge)


def _write_ontology(path: Path, *, classes: list[dict] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "classes": classes or [],
                "edges": [],
                "attributes": [],
            }
        ),
        encoding="utf-8",
    )


def test_folder_output_path_uses_snake_case_folder_name(tmp_path):
    assert (
        rdf_merge.folder_output_path(tmp_path / "LoansGeneral").name
        == "all_loans_general.ontology.json"
    )
    assert (
        rdf_merge.folder_output_path(tmp_path / "RealEstateLoans").name
        == "all_real_estate_loans.ontology.json"
    )


def test_write_merged_ontology_folders_merges_nested_folders_bottom_up(tmp_path):
    be = tmp_path / "BE"
    corporations = be / "Corporations"
    nested = corporations / "Nested"
    loan = tmp_path / "LOAN"
    _write_ontology(
        be / "AllBE.ontology.json",
        classes=[{"iri": "be-root"}, {"iri": "shared"}],
    )
    _write_ontology(
        corporations / "Corporations.ontology.json",
        classes=[{"iri": "corporation"}, {"iri": "shared"}],
    )
    _write_ontology(
        nested / "NestedCorporation.ontology.json",
        classes=[{"iri": "nested-corporation"}],
    )
    _write_ontology(
        loan / "AllLOAN.ontology.json",
        classes=[{"iri": "loan-root"}, {"iri": "shared"}],
    )

    results = rdf_merge.write_merged_ontology_folders(tmp_path)

    outputs = {Path(item["output"]).name: item for item in results}
    assert set(outputs) == {
        "all.ontology.json",
        "all_be.ontology.json",
        "all_corporations.ontology.json",
        "all_loan.ontology.json",
        "all_nested.ontology.json",
    }

    corporations_data = json.loads(
        (corporations / "all_corporations.ontology.json").read_text(encoding="utf-8")
    )
    assert corporations_data["summary"]["sourceFiles"] == 2
    assert {item["iri"] for item in corporations_data["classes"]} == {
        "corporation",
        "shared",
        "nested-corporation",
    }

    be_data = json.loads((be / "all_be.ontology.json").read_text(encoding="utf-8"))
    assert be_data["summary"]["sourceFiles"] == 2
    assert {Path(source_file).name for source_file in be_data["sourceFiles"]} == {
        "AllBE.ontology.json",
        "all_corporations.ontology.json",
    }
    assert {item["iri"] for item in be_data["classes"]} == {
        "be-root",
        "shared",
        "corporation",
        "nested-corporation",
    }
    assert (
        len([item for item in be_data["classes"] if item["iri"] == "shared"])
        == 1
    )

    root_data = json.loads((tmp_path / "all.ontology.json").read_text(encoding="utf-8"))
    assert root_data["summary"]["sourceFiles"] == 2
    assert {Path(source_file).name for source_file in root_data["sourceFiles"]} == {
        "all_be.ontology.json",
        "all_loan.ontology.json",
    }
    assert {item["iri"] for item in root_data["classes"]} == {
        "be-root",
        "corporation",
        "loan-root",
        "nested-corporation",
        "shared",
    }
    assert (
        len([item for item in root_data["classes"] if item["iri"] == "shared"])
        == 1
    )


def test_load_all_ontologies_skips_existing_lowercase_outputs(tmp_path):
    folder = tmp_path / "BE" / "Corporations"
    _write_ontology(
        folder / "Corporations.ontology.json",
        classes=[{"iri": "corporation"}],
    )
    _write_ontology(
        folder / "all_corporations.ontology.json",
        classes=[{"iri": "old-output"}],
    )
    _write_ontology(
        folder / "all.ontology.json",
        classes=[{"iri": "old-root-output"}],
    )

    merged = rdf_merge.load_all_ontologies(
        folder,
        folder / "all_corporations.ontology.json",
    )

    assert merged["summary"]["sourceFiles"] == 1
    assert merged["classes"] == [{"iri": "corporation"}]
