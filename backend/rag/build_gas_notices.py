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
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CSV_PATH = REPO_ROOT / "data" / "docs" / "Gas Pipeline.csv"
OUT_PATH = REPO_ROOT / "data" / "docs" / "gas-pipeline-notices.md"
SOURCE_URL = "https://www.misoenergy.org/markets-and-operations/notifications/gas-pipeline/"

# Older notices are history, not operating conditions. 90 days keeps a season
# of context without burying the current picture.
RECENT_DAYS = 90
# Per pipeline, so one busy operator cannot crowd out the rest.
MAX_PER_PIPELINE = 12


# A subject long enough to fill a chunk on its own crowds the doc lane the same
# way too many notices would - just by width instead of by count.
MAX_SUBJECT_CHARS = 300

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


def clean_text(value: str, limit: int = MAX_SUBJECT_CHARS) -> str:
    """One line of plain text, safe to place inside markdown.

    This CSV is republished from a couple of dozen third-party pipeline
    operators' systems, and it does arrive damaged - one live row carries a raw
    0xAC byte where an ampersand belongs. So treat every free-text field as
    hostile: newlines would forge headings in a document the assistant cites,
    and square brackets would forge links. Neutralize both rather than trust
    the feed.
    """
    text = _WHITESPACE.sub(" ", _CONTROL.sub(" ", value or "")).strip()
    text = text.replace("[", "(").replace("]", ")")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def safe_url(value: str) -> str | None:
    """The URL if it is a plain http(s) link, otherwise None.

    A `startswith("http")` check is not enough: it admits "http:evil", and it
    does nothing about a `)` in the middle of the value, which closes the
    markdown link early and lets the rest of the field become a second link
    the reader never sees coming.
    """
    url = (value or "").strip()
    if not url or _CONTROL.search(url) or any(c in url for c in ' ()[]<>"'):
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return url


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
    """Each pipeline's newest notices from the last RECENT_DAYS, deduplicated.

    The export repeats notices - ANR's twelve slots held the same "UPDATED:
    CAPACITY REDUCTION Southwest Area" three times - so a quarter of that
    pipeline's budget was spent restating one notice while a distinct one fell
    off the end. Dedupe before the cap, not after.
    """
    cutoff = now - timedelta(days=RECENT_DAYS)
    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        posted = parse_dt(row.get("Posted (EST)", ""))
        if posted is None or posted < cutoff:
            continue
        # a pipeline name lands in a "## " heading, so strip the one character
        # that could make it read as further structure
        pipeline = (clean_text(row.get("Pipeline", ""), limit=60)
                    .replace("#", "").strip() or "Unidentified pipeline")
        grouped[pipeline].append((posted, row))
    for pipeline, entries in grouped.items():
        entries.sort(key=lambda t: t[0], reverse=True)
        seen: set = set()
        unique = []
        for posted, row in entries:
            key = (clean_text(row.get("Type", "")), clean_text(row.get("Subject", "")))
            if key in seen:
                continue
            seen.add(key)
            unique.append((posted, row))
        grouped[pipeline] = unique[:MAX_PER_PIPELINE]
    return grouped


def build(rows: list[dict], now: datetime) -> tuple[str, int, int]:
    """The document, plus how many notices and pipelines it covers."""
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
            subject = clean_text(row.get("Subject", "")) or "(no subject)"
            kind = clean_text(row.get("Type", ""), limit=60) or "Notice"
            effective = parse_dt(row.get("Effective (EST)", ""))
            end = parse_dt(row.get("End (EST)", ""))
            # an End with no Effective used to vanish silently; report whichever
            # of the two the row actually carries
            window = ""
            if effective and end:
                window = (f", effective {effective:%d %b %Y %H:%M} EST"
                          f" until {end:%d %b %Y %H:%M} EST")
            elif effective:
                window = f", effective {effective:%d %b %Y %H:%M} EST"
            elif end:
                window = f", until {end:%d %b %Y %H:%M} EST"
            url = safe_url(row.get("Notice Url", ""))
            link = f" [notice]({url})" if url else ""
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
