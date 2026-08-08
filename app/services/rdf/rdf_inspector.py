from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

# pyrefly: ignore [missing-import]
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

RULES = (
    (OWL.someValuesFrom, "some"),
    (OWL.allValuesFrom, "only"),
    (OWL.hasValue, "value"),
    (OWL.minCardinality, "min"),
    (OWL.maxCardinality, "max"),
    (OWL.cardinality, "exactly"),
    (OWL.minQualifiedCardinality, "minQualified"),
    (OWL.maxQualifiedCardinality, "maxQualified"),
    (OWL.qualifiedCardinality, "exactlyQualified"),
)

RDF_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "rdf"
ONTOLOGY_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "ontology"
DEFAULT_INPUT_PATH = RDF_DATA_DIR / "LoanApplications.rdf"
DEFAULT_RDF_FORMAT = "xml"
RDF_FORMAT_BY_SUFFIX = {
    ".rdf": "xml",
    ".owl": "xml",
    ".xml": "xml",
    ".ttl": "turtle",
    ".nt": "nt",
    ".n3": "n3",
    ".jsonld": "json-ld",
}
CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def label(g: Graph, subject, lang: str) -> str | None:
    """Return the best human label for an RDF subject."""

    values = [x for x in g.objects(subject, RDFS.label) if isinstance(x, Literal)]
    return next(
        (str(x) for x in values if x.language == lang),
        next(
            (str(x) for x in values if x.language is None),
            str(values[0]) if values else None,
        ),
    )


def definition(g: Graph, subject, lang: str) -> str | None:
    """Return a plain-language definition from SKOS or RDFS metadata."""

    for predicate in (SKOS.definition, RDFS.comment):
        values = [x for x in g.objects(subject, predicate) if isinstance(x, Literal)]
        preferred = next((str(x) for x in values if x.language == lang), None)
        if preferred:
            return preferred
        if values:
            return str(values[0])
    return None


def short(g: Graph, term) -> str:
    """Compact an RDF term into a CURIE-like technical name when possible."""

    if term is None:
        return "-"
    if isinstance(term, Literal):
        return str(term)
    if isinstance(term, BNode):
        return f"_:{term}"
    try:
        return g.namespace_manager.normalizeUri(term)
    except Exception:
        return str(term)


def local_name(term) -> str | None:
    """Extract the final local identifier from a URIRef."""

    if not isinstance(term, URIRef):
        return None
    uri = str(term).rstrip("/#")
    separator = "#" if "#" in uri else "/"
    return uri.rsplit(separator, 1)[-1] or None


def humanize_name(value: str) -> str:
    """Turn technical identifiers such as LoanSpecificAccount into readable text."""

    value = value.replace("_", " ").replace("-", " ")
    value = CAMEL_CASE_BOUNDARY.sub(" ", value)
    return " ".join(value.split()).lower()


def display_term(g: Graph, term, lang: str, prefer_label: bool = False) -> str:
    """Render RDF terms for reports without exposing noisy default-prefix colons."""

    if term is None:
        return "-"
    if isinstance(term, Literal):
        return str(term)
    if isinstance(term, BNode):
        return "anonymous restriction"

    term_label = label(g, term, lang) if isinstance(term, URIRef) else None
    if prefer_label and term_label:
        return term_label

    curie = short(g, term)
    if curie.startswith(":"):
        name = local_name(term)
        return humanize_name(name) if name else str(term)
    if term_label:
        return term_label
    return curie


def detect_base(g: Graph) -> str | None:
    """Detect the ontology base IRI from owl:Ontology declarations."""

    ontology = next(
        (x for x in g.subjects(RDF.type, OWL.Ontology) if isinstance(x, URIRef)),
        None,
    )
    return str(ontology) if ontology else None


def is_local(term, base: str | None) -> bool:
    """Check whether a URIRef belongs to the selected ontology namespace."""

    if base is None or not isinstance(term, URIRef):
        return False
    root = base.rstrip("/#")
    uri = str(term)
    return uri == base or uri.startswith(root + "/") or uri.startswith(root + "#")


def find_restrictions(g: Graph, cls: URIRef, lang: str) -> list[dict[str, str | None]]:
    """Collect OWL restriction rules attached to a class."""

    roots = list(g.objects(cls, RDFS.subClassOf))
    roots += list(g.objects(cls, OWL.equivalentClass))

    stack = [x for x in roots if isinstance(x, BNode)]
    visited: set[BNode] = set()
    found: list[dict] = []

    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)

        if (node, RDF.type, OWL.Restriction) in g:
            prop = g.value(node, OWL.onProperty)
            for predicate, operator in RULES:
                value = g.value(node, predicate)
                if value is not None:
                    qualifier = g.value(node, OWL.onClass)
                    if qualifier is None:
                        qualifier = g.value(node, OWL.onDataRange)
                    found.append(
                        {
                            "property": display_term(g, prop, lang, prefer_label=True),
                            "operator": operator,
                            "value": display_term(g, value, lang),
                            "qualifier": (
                                display_term(g, qualifier, lang) if qualifier else None
                            ),
                        }
                    )
                    break

        stack.extend(
            x for x in g.objects(node) if isinstance(x, BNode) and x not in visited
        )

    return found


def resolve_input_path(input_path: Path | str | None = None) -> Path:
    """Resolve RDF input paths relative to app/data/rdf when needed."""

    path = Path(input_path) if input_path else DEFAULT_INPUT_PATH
    if not path.is_absolute():
        candidate = RDF_DATA_DIR / path
        path = candidate if candidate.exists() else path

    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"RDF input file not found: {path}")
    if not path.is_file():
        raise ValueError(f"RDF input path must be a file: {path}")
    return path


def default_output_path(input_path: Path) -> Path:
    """Build the default JSON output path under app/data/ontology."""

    return ONTOLOGY_DATA_DIR / f"{input_path.stem}.ontology.json"


def resolve_output_path(output_path: Path | str | None, input_path: Path) -> Path:
    """Resolve and prepare the JSON output path used by the CLI."""

    path = Path(output_path) if output_path else default_output_path(input_path)
    path = path if path.is_absolute() else path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def detect_rdf_format(path: Path, explicit_format: str | None = None) -> str:
    """Choose the rdflib parser format from CLI input or file extension."""

    if explicit_format:
        return explicit_format
    return RDF_FORMAT_BY_SUFFIX.get(path.suffix.lower(), DEFAULT_RDF_FORMAT)


def inspect(
    path: Path | str | None = None,
    rdf_format: str | None = None,
    base: str | None = None,
    include_all: bool = False,
    lang: str = "en",
) -> dict[str, Any]:
    """Parse an RDF/OWL file into ontology nodes, edges, and attributes."""

    path = resolve_input_path(path)
    rdf_format = detect_rdf_format(path, rdf_format)

    g = Graph()
    g.parse(path, format=rdf_format)

    base = base or detect_base(g)

    def allowed(term) -> bool:
        return include_all or is_local(term, base)

    classes = []
    for cls in sorted(
        {
            x
            for x in g.subjects(RDF.type, OWL.Class)
            if isinstance(x, URIRef) and allowed(x)
        },
        key=str,
    ):
        classes.append(
            {
                "name": display_term(g, cls, lang, prefer_label=True),
                "technicalName": short(g, cls),
                "localName": local_name(cls),
                "iri": str(cls),
                "label": label(g, cls, lang),
                "definition": definition(g, cls, lang),
                "parents": [
                    display_term(g, x, lang, prefer_label=True)
                    for x in g.objects(cls, RDFS.subClassOf)
                    if isinstance(x, URIRef)
                ],
                "rules": find_restrictions(g, cls, lang),
            }
        )

    edges = []
    attributes = []
    for rdf_type, kind, target in (
        (OWL.ObjectProperty, "edge", edges),
        (OWL.DatatypeProperty, "property", attributes),
    ):
        terms = sorted(
            {
                x
                for x in g.subjects(RDF.type, rdf_type)
                if isinstance(x, URIRef) and allowed(x)
            },
            key=str,
        )
        for prop in terms:
            target.append(
                {
                    "kind": kind,
                    "name": display_term(g, prop, lang, prefer_label=True),
                    "technicalName": short(g, prop),
                    "localName": local_name(prop),
                    "iri": str(prop),
                    "label": label(g, prop, lang),
                    "definition": definition(g, prop, lang),
                    "domain": [
                        display_term(g, x, lang, prefer_label=True)
                        for x in g.objects(prop, RDFS.domain)
                    ],
                    "range": [
                        display_term(g, x, lang, prefer_label=True)
                        for x in g.objects(prop, RDFS.range)
                    ],
                }
            )

    summary = {
        "nodes": len(classes),
        "edges": len(edges),
        "properties": len(attributes),
        "triples": len(g),
    }

    return {
        "file": str(path),
        "ontologyBase": base,
        "summary": summary,
        "imports": [str(x) for x in g.objects(None, OWL.imports)],
        "classes": classes,
        "edges": edges,
        "attributes": attributes,
    }


def print_report(data: dict) -> None:
    """Print a readable ontology summary for humans at the terminal."""

    print("RDF ontology report")
    print(f"Source file: {data['file']}")
    print(f"Base IRI: {data['ontologyBase'] or '(not detected)'}")
    print(
        "Summary: "
        f"{data['summary']['nodes']} nodes, "
        f"{data['summary']['edges']} edges, "
        f"{data['summary']['properties']} properties, "
        f"{data['summary']['triples']} triples"
    )

    print(f"\nClasses / nodes ({len(data['classes'])})")
    for item in data["classes"]:
        print(f"\n- {item['name']}")
        if item["definition"]:
            print(f"  Meaning: {item['definition']}")
        if item["parents"]:
            print(f"  Parent classes: {', '.join(item['parents'])}")
        if item["rules"]:
            print("  Rules:")
            for rule in item["rules"]:
                qualifier = f" on {rule['qualifier']}" if rule["qualifier"] else ""
                print(
                    f"    - {rule['property']} {rule['operator']} "
                    f"{rule['value']}{qualifier}"
                )

    for items, title in (
        (data["edges"], "Object properties / edges"),
        (data["attributes"], "Datatype properties / attributes"),
    ):
        print(f"\n{title} ({len(items)})")
        for item in items:
            print(f"\n- {item['name']}")
            print(f"  Domain: {', '.join(item['domain']) or '-'}")
            print(f"  Range: {', '.join(item['range']) or '-'}")
            if item["definition"]:
                print(f"  Meaning: {item['definition']}")


def main() -> None:
    """Run the RDF inspector CLI and save the ontology JSON output."""

    parser = argparse.ArgumentParser(description="Inspect an RDF/OWL ontology")
    parser.add_argument(
        "file",
        nargs="?",
        type=Path,
        help="Backward-compatible RDF file path. Prefer --input-path.",
    )
    parser.add_argument(
        "--input-path",
        type=Path,
        default=None,
        help=(
            "RDF file to inspect. Relative paths are resolved from app/data/rdf. "
            f"Default: {DEFAULT_INPUT_PATH.name}"
        ),
    )
    parser.add_argument(
        "--format",
        default=None,
        help="RDF parser format. When omitted, inferred from the file extension.",
    )
    parser.add_argument("--base", help="Override ontology namespace/base IRI")
    parser.add_argument("--all", action="store_true", help="Include external terms")
    parser.add_argument("--lang", default="en")
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help=(
            "JSON output path. Defaults to app/data/ontology/"
            "<input-file-name>.ontology.json"
        ),
    )
    args = parser.parse_args()

    input_path = args.input_path or args.file
    resolved_input_path = resolve_input_path(input_path)
    data = inspect(resolved_input_path, args.format, args.base, args.all, args.lang)
    print_report(data)

    output_path = resolve_output_path(args.json, resolved_input_path)
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nJSON saved: {output_path}")


if __name__ == "__main__":
    main()
