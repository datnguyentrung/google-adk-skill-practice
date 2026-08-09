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


def test_write_merged_ontology_folders_merges_each_folder_direct_inputs(tmp_path):
    loans_general = tmp_path / "LOAN" / "LoansGeneral"
    nested = loans_general / "Nested"
    _write_ontology(
        loans_general / "LoanApplications.ontology.json",
        classes=[{"iri": "loan-application"}],
    )
    _write_ontology(
        loans_general / "Loans.ontology.json",
        classes=[{"iri": "loan"}],
    )
    _write_ontology(
        nested / "NestedLoan.ontology.json",
        classes=[{"iri": "nested-loan"}],
    )

    results = rdf_merge.write_merged_ontology_folders(tmp_path)

    outputs = {Path(item["output"]).name: item for item in results}
    assert set(outputs) == {
        "all_loans_general.ontology.json",
        "all_nested.ontology.json",
    }

    loans_general_data = json.loads(
        (loans_general / "all_loans_general.ontology.json").read_text(
            encoding="utf-8"
        )
    )
    assert loans_general_data["summary"]["sourceFiles"] == 2
    assert loans_general_data["summary"]["classes"] == 2
    assert all(
        Path(source_file).parent == loans_general
        for source_file in loans_general_data["sourceFiles"]
    )


def test_load_all_ontologies_skips_existing_all_files(tmp_path):
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
        folder / "AllCorporations.ontology.json",
        classes=[{"iri": "old-all-output"}],
    )

    merged = rdf_merge.load_all_ontologies(
        folder,
        folder / "all_corporations.ontology.json",
    )

    assert merged["summary"]["sourceFiles"] == 1
    assert merged["classes"] == [{"iri": "corporation"}]
