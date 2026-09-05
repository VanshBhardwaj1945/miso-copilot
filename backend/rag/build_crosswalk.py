"""Draft the report-to-API crosswalk: old market-report columns -> Data Exchange fields.

Run:  python -m backend.rag.build_crosswalk            (writes crosswalk.draft.json)
      python -m backend.rag.build_crosswalk --promote  (draft -> crosswalk.json)

MISO is retiring its CSV market reports in favor of the Data Exchange API, and
nobody published which old column became which new field. This script has
Claude draft that table, then checks every answer against the API spec:

  readers' guide text (old side)  +  OpenAPI spec (new side)
        -> Claude proposes entries as JSON
        -> validator drops any endpoint or field that is not literally in the spec
        -> crosswalk.draft.json, for a human to read and promote

"AI drafts, human approves." The validator is what makes that safe: Claude can
only choose among real field names, never invent one. A wrong mapping would
send a trader to the wrong column, which is worse than no crosswalk at all.

Specs come from data/specs/ (gitignored). The official ones are the OpenAPI 3
files downloadable from data-exchange.misoenergy.org once signed in (User
Guide, step 7). Until then, fetch_specs() pulls a public reconstruction.
"""

import json
import re
import sys
import time
from pathlib import Path

import requests
import yaml
from pydantic import BaseModel

from backend.config import CLAUDE_API_KEY, MODEL

HERE = Path(__file__).resolve().parent
DOCS_DIR = HERE.parent.parent / "data" / "docs"
SPECS_DIR = HERE.parent.parent / "data" / "specs"
DRAFT_PATH = HERE / "crosswalk.draft.json"
FINAL_PATH = HERE / "crosswalk.json"

# Public reconstructions of the two Data Exchange specs (api-evangelist). Drop
# the official YAML from the portal into data/specs/ and these are ignored.
SPEC_SOURCES = {
    "pricing": "https://raw.githubusercontent.com/api-evangelist/miso/main/openapi/"
               "miso-data-exchange-pricing-api-openapi.json",
    "lgi": "https://raw.githubusercontent.com/api-evangelist/miso/main/openapi/"
           "miso-data-exchange-load-generation-interchange-api-openapi.json",
}

# Which readers' guides to draft from. Each guide can describe several reports;
# Claude emits one entry per report that has an API equivalent.
GUIDES = [
    "readers-guide-real-time-final-lmps.pdf",
    "readers-guide-day-ahead-pricing.pdf",
    "readers-guide-generation-fuel-mix.pdf",
]

MAX_GUIDE_CHARS = 14000


# --- new side: the spec -----------------------------------------------------

def fetch_specs() -> None:
    """Download the public spec reconstructions if data/specs/ has nothing."""
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    if any(SPECS_DIR.glob("*.json")) or any(SPECS_DIR.glob("*.y*ml")):
        return
    for name, url in SPEC_SOURCES.items():
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        (SPECS_DIR / f"{name}.json").write_bytes(response.content)
        print(f"      fetched  data/specs/{name}.json")
        time.sleep(1)


def _field_paths(schema: dict, schemas: dict, prefix: str = "") -> list[str]:
    """Every dotted property path in a response schema: node, timeInterval.start, ..."""
    if not isinstance(schema, dict):
        return []
    if "$ref" in schema:
        return _field_paths(schemas.get(schema["$ref"].split("/")[-1], {}), schemas, prefix)
    if schema.get("type") == "array":
        return _field_paths(schema.get("items", {}), schemas, prefix)
    paths = []
    for name, sub in schema.get("properties", {}).items():
        full = f"{prefix}{name}"
        paths.append(full)
        paths.extend(_field_paths(sub, schemas, full + "."))
    return paths


def load_endpoints() -> dict[str, dict]:
    """'GET /v1/...' -> {api, base, params, fields} for every spec in data/specs/."""
    endpoints = {}
    for path in sorted(list(SPECS_DIR.glob("*.json")) + list(SPECS_DIR.glob("*.y*ml"))):
        text = path.read_text(encoding="utf-8")
        spec = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
        schemas = spec.get("components", {}).get("schemas", {})
        base = (spec.get("servers") or [{}])[0].get("url", "")
        api = base.rstrip("/").split("/")[-1] or path.stem
        for route, ops in spec.get("paths", {}).items():
            for method, op in ops.items():
                if not isinstance(op, dict) or "responses" not in op:
                    continue
                fields = []
                for body in op["responses"].get("200", {}).get("content", {}).values():
                    fields = _field_paths(body.get("schema", {}), schemas)
                # the API wraps rows in data[] and paging in page{}; callers care about rows
                fields = [f.removeprefix("data.") for f in fields if f.startswith("data.")]
                params = [p["name"] for p in op.get("parameters", [])
                          if isinstance(p, dict) and "name" in p]
                endpoints[f"{method.upper()} {route}"] = {
                    "api": api, "base": base, "params": params, "fields": fields,
                }
    return endpoints


# --- old side: the readers' guides ------------------------------------------

def guide_text(filename: str) -> str:
    """The readers' guide as plain text, trimmed to what fits a prompt."""
    from pypdf import PdfReader
    reader = PdfReader(str(DOCS_DIR / filename))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return re.sub(r"[ \t]+", " ", text)[:MAX_GUIDE_CHARS]


def guide_url(filename: str) -> str:
    """The citation URL for a guide, from doc_sources.json."""
    for src in json.loads((HERE / "doc_sources.json").read_text(encoding="utf-8")):
        if src["file"] == filename:
            return src["url"]
    return ""


# --- Claude drafts ----------------------------------------------------------

class FieldMap(BaseModel):
    old: str
    new: str
    note: str = ""


class Entry(BaseModel):
    report: str
    old_file: str = ""
    endpoint: str
    params: dict[str, str] = {}
    fields: list[FieldMap]
    shape: str = ""


class Draft(BaseModel):
    entries: list[Entry]


DRAFT_PROMPT = """You are building a crosswalk from MISO's retiring CSV market reports to its \
new Data Exchange API. Below is one readers' guide (describes the OLD reports and \
their columns) and the catalog of NEW API endpoints with their exact field names.

For every distinct report in the guide that has an API equivalent, emit one entry. \
Use ONLY endpoint keys and field paths that appear verbatim in the catalog. If a \
column has no equivalent, leave it out rather than guessing. Old column names must \
come from the guide (or MISO's known CSV headers: Node, Type, Value, HE 1..HE 24, \
MARKET_DAY, INTERVALEST).

Per entry: report (as the guide names it), old_file (pattern if given, else ''), \
endpoint (one catalog key, e.g. GET /v1/real-time/{date}/lmp-expost), params (the \
values that select this report's data), fields (old column or 'Value = LMP' style \
row label -> field path from the catalog, plus a short note), and shape (one \
sentence on any wide->long or row->field change).

=== READERS' GUIDE: <<GUIDE_NAME>> ===
<<GUIDE>>

=== API CATALOG ===
<<CATALOG>>
"""


def draft_from_guide(client, filename: str, endpoints: dict) -> list[dict]:
    """Ask Claude for crosswalk entries grounded in one guide plus the spec."""
    catalog = "\n".join(
        f"{key}  params={ep['params']}  fields={ep['fields']}" for key, ep in endpoints.items()
    )
    # str.replace, not str.format: the template holds literal JSON braces
    prompt = (DRAFT_PROMPT.replace("<<GUIDE_NAME>>", filename)
              .replace("<<GUIDE>>", guide_text(filename))
              .replace("<<CATALOG>>", catalog))
    # structured output: the API guarantees the reply matches Draft, so there is
    # no JSON to find, trim, or repair - a malformed reply cannot happen
    response = client.messages.parse(model=MODEL, max_tokens=16000,
                                     messages=[{"role": "user", "content": prompt}],
                                     output_format=Draft)
    if response.stop_reason == "refusal" or response.parsed_output is None:
        print(f"   no draft for {filename} (stop_reason={response.stop_reason})")
        return []
    entries = [e.model_dump() for e in response.parsed_output.entries]
    for entry in entries:
        entry["guide"] = filename
        entry["report_url"] = guide_url(filename)
    return entries


# --- the validator ----------------------------------------------------------

def validate(entry: dict, endpoints: dict) -> tuple[dict | None, list[str]]:
    """Keep only what the spec can vouch for. Returns (entry or None, warnings)."""
    warnings = []
    ep = endpoints.get(entry.get("endpoint", ""))
    if ep is None:
        return None, [f"{entry.get('report')}: endpoint {entry.get('endpoint')!r} not in spec - dropped"]

    allowed = set(ep["fields"]) | {f"param:{p}" for p in ep["params"]}
    kept = []
    for f in entry.get("fields", []):
        if f.get("new") in allowed:
            kept.append(f)
        else:
            warnings.append(f"{entry['report']}: field {f.get('new')!r} not on {entry['endpoint']} - dropped")
    if not kept:
        return None, warnings + [f"{entry['report']}: no verifiable fields - dropped"]

    bad_params = [p for p in entry.get("params", {}) if p not in ep["params"]]
    for p in bad_params:
        warnings.append(f"{entry['report']}: param {p!r} not on endpoint - dropped")
        entry["params"].pop(p)

    entry["fields"] = kept
    entry["api"] = ep["api"]
    entry["base_url"] = ep["base"]
    entry["status"] = "draft"
    return entry, warnings


# --- entry point ------------------------------------------------------------

def build_draft() -> int:
    import anthropic
    if not CLAUDE_API_KEY:
        sys.exit("CLAUDE_API_KEY is not set - the draft step needs Claude")
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    fetch_specs()
    endpoints = load_endpoints()
    print(f"spec: {len(endpoints)} endpoints across {sorted({e['api'] for e in endpoints.values()})}")

    entries, warnings = [], []
    for filename in GUIDES:
        if not (DOCS_DIR / filename).exists():
            warnings.append(f"{filename} missing - run fetch_docs first")
            continue
        drafted = draft_from_guide(client, filename, endpoints)
        for entry in drafted:
            kept, notes = validate(entry, endpoints)
            warnings.extend(notes)
            if kept:
                entries.append(kept)
        print(f"{len(drafted):>3} drafted  {filename}")

    DRAFT_PATH.write_text(json.dumps({
        "note": "DRAFT - drafted by Claude, checked against the spec, not yet human-reviewed. "
                "Review, then: python -m backend.rag.build_crosswalk --promote",
        "spec_files": [p.name for p in sorted(SPECS_DIR.iterdir())],
        "entries": entries,
    }, indent=2), encoding="utf-8")

    print(f"\n{len(entries)} entries kept -> {DRAFT_PATH.name}")
    for w in warnings:
        print("   !", w)
    return len(entries)


def promote() -> None:
    """Draft -> crosswalk.json, marking every entry as human-reviewed."""
    data = json.loads(DRAFT_PATH.read_text(encoding="utf-8"))
    for entry in data["entries"]:
        entry["status"] = "reviewed"
    data["note"] = "Report-to-API crosswalk. Drafted by Claude from the readers' guides and the " \
                   "Data Exchange spec, every field checked against the spec, reviewed by a human."
    FINAL_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"{len(data['entries'])} entries -> {FINAL_PATH.name}")


if __name__ == "__main__":
    if "--promote" in sys.argv:
        promote()
    else:
        build_draft()
