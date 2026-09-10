"""Build gas-pipeline-notices.md from MISO's Gas Pipeline notices export.

MISO republishes interstate pipeline operators' notices - maintenance, capacity
constraints, critical notices - because a gas pipeline restriction is a
generation-availability problem for the grid.

Run:  python -m backend.rag.build_gas_notices

The CSV is 1,151 rows spanning 2005-2026. Ingesting it raw would be the mistake
transformers.py exists to avoid: comma-delimited rows embed badly, and a
document per notice would put a thousand near-identical entries in a doc lane
with four seats. So this keeps only recent notices, groups them by pipeline,
and writes one readable paragraph per pipeline - the shape someone would ask
the question in.
"""

import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CSV_PATH = REPO_ROOT / "data" / "docs" / "Gas Pipeline.csv"
OUT_PATH = REPO_ROOT / "data" / "docs" / "gas-pipeline-notices.md"
SOURCE_URL = "https://www.misoenergy.org/markets-and-operations/notifications/gas-pipeline/"

# Older notices are history, not operating conditions. 90 days keeps a season
# of context without burying the current picture.
RECENT_DAYS = 90
# Per pipeline, so one busy operator cannot crowd out the rest.
MAX_PER_PIPELINE = 12


def parse_dt(value: str):
    """MISO's export uses 'MM/DD/YYYY H:MM AM'. Unparseable means undated."""
    try:
        return datetime.strptime((value or "").strip(), "%m/%d/%Y %I:%M %p")
    except ValueError:
        return None


def load(path: Path) -> list[dict]:
    # utf-8-sig: the export carries a BOM, which would otherwise corrupt the
    # first column name and silently drop every Pipeline value
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def recent_by_pipeline(rows: list[dict], now: datetime) -> dict[str, list]:
    cutoff = now - timedelta(days=RECENT_DAYS)
    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        posted = parse_dt(row.get("Posted (EST)", ""))
        if posted is None or posted < cutoff:
            continue
        pipeline = (row.get("Pipeline") or "").strip() or "Unidentified pipeline"
        grouped[pipeline].append((posted, row))
    for pipeline in grouped:
        grouped[pipeline].sort(key=lambda t: t[0], reverse=True)
        del grouped[pipeline][MAX_PER_PIPELINE:]
    return grouped


def build(rows: list[dict], now: datetime) -> tuple[str, int, int]:
    grouped = recent_by_pipeline(rows, now)
    lines = [
        "# MISO gas pipeline notices",
        "",
        "Notices from the interstate gas pipelines serving MISO's footprint -",
        "maintenance, capacity constraints, operational flow orders and critical",
        "notices. A pipeline restriction limits what gas generation can run, so",
        "these are grid conditions, not just gas news.",
        "",
        f"Generated {now:%Y-%m-%d} by backend/rag/build_gas_notices.py from MISO's",
        f"Gas Pipeline notices export. Covers the last {RECENT_DAYS} days; the full",
        f"archive lives at <{SOURCE_URL}>.",
        "",
    ]
    kept = 0
    for pipeline in sorted(grouped, key=lambda p: (-len(grouped[p]), p)):
        entries = grouped[pipeline]
        lines.append(f"## {pipeline}")
        lines.append("")
        lines.append(f"{len(entries)} recent notice{'s' if len(entries) != 1 else ''}:")
        lines.append("")
        for posted, row in entries:
            subject = (row.get("Subject") or "").strip() or "(no subject)"
            kind = (row.get("Type") or "").strip() or "Notice"
            effective = parse_dt(row.get("Effective (EST)", ""))
            end = parse_dt(row.get("End (EST)", ""))
            window = ""
            if effective:
                window = f", effective {effective:%d %b %Y %H:%M} EST"
                if end:
                    window += f" until {end:%d %b %Y %H:%M} EST"
            url = (row.get("Notice Url") or "").strip()
            link = f" [notice]({url})" if url.startswith("http") else ""
            lines.append(f"- **{kind}** - {subject} (posted {posted:%d %b %Y}{window}).{link}")
            kept += 1
        lines.append("")
    return "\n".join(lines) + "\n", kept, len(grouped)


def main() -> None:
    if not CSV_PATH.exists():
        sys.exit(f"No CSV at {CSV_PATH}")
    rows = load(CSV_PATH)
    now = datetime.now()
    markdown, kept, pipelines = build(rows, now)
    OUT_PATH.write_text(markdown, encoding="utf-8")
    print(f"{len(rows)} notices in the export -> {kept} kept "
          f"({RECENT_DAYS} days, max {MAX_PER_PIPELINE} per pipeline) "
          f"across {pipelines} pipelines")
    print(f"Wrote {OUT_PATH} ({len(markdown) / 1024:.1f} KB)")
    print("Re-run `python -m backend.rag.ingest_docs` (backend stopped) to index it.")


if __name__ == "__main__":
    main()
