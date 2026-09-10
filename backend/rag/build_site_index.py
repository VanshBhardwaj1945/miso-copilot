"""Build site_index.md - a map of which misoenergy.org page covers which topic.

The assistant's most common job is "where do I find X?", and for that the link is
the answer. This turns MISO's published sitemap into a short, readable index the
doc lane can retrieve from, so the answer carries a real URL instead of a guess.

Run:  python -m backend.rag.build_site_index

Reads a local export of MISO's published sitemap - data/docs/misoenergy-sitemap.txt,
saved by hand. This module makes no network requests at all, and the URL list it
reads is NOT a license to fetch those pages: miso.org bans scrapers (see
AGENTS.md). Only the page names are used, derived from the URL paths; no page is
ever visited.

Most of the sitemap is not worth indexing: events and stakeholder engagement
are 86 percent of its 2,595 URLs, and a meeting page from 2023 is never the
answer to "where do I find X?". Dropping those, news releases and dated
notification posts leaves 209 pages that are.

Like crosswalk.json, the output is committed. It cannot be fetched per-file the
way doc_sources.json entries are, so it ships in the repo and teammates get it
from a clone rather than a build step they have to remember.
"""

import datetime
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SITEMAP_PATH = REPO_ROOT / "data" / "docs" / "misoenergy-sitemap.txt"
SITEMAP_URL = "https://www.misoenergy.org/sitemap.xml"
SITE_HOST = "misoenergy.org"
OUT_PATH = Path(__file__).resolve().parent / "site_index.md"

# One-off content rather than places to send someone - see the docstring.
DROP_SECTIONS = {"events", "engage", "extranet", "account", "manage",
                 "search", "past-events"}

# Dated posts inside otherwise useful sections - same reasoning.
DROP_IF_CONTAINS = ("news-releases", "media-center/20")

SECTION_TITLES = {
    "markets-and-operations": "Markets and Operations - real-time data, market reports, pricing",
    "meet-miso": "Meet MISO - about, media center, careers",
    "planning": "Planning - generator interconnection, MTEP, resource adequacy",
    "legal": "Legal - tariff, FERC filings, agreements",
    "forms": "Forms",
    "library": "Library",
}

# Path segments that a naive .capitalize() would mangle into nonsense.
ACRONYMS = {
    "lmp": "LMP", "ftr": "FTR", "arr": "ARR", "arrftr": "ARR/FTR", "mtep": "MTEP",
    "rtc": "RTC", "asm": "ASM", "miso": "MISO", "ferc": "FERC", "pra": "PRA",
    "rfp": "RFP", "api": "API", "faq": "FAQ", "dart": "DART", "cpnode": "CPNode",
    "irp": "IRP", "rsp": "RSP", "mpma": "MPMA", "rto": "RTO", "iso": "ISO",
    "oms": "OMS", "mse": "MSE", "mp": "MP",
}


def read_sitemap(path: Path = SITEMAP_PATH) -> list[str]:
    """Every URL in the saved sitemap export.

    The file is MISO's sitemap saved to disk: a header, then one URL per line
    with an optional tab-separated lastmod date. Reading it rather than
    fetching keeps this module entirely offline, so regenerating the index
    can never put a request on misoenergy.org.
    """
    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.split("\t")[0].strip()
        if candidate.startswith("http"):
            urls.append(candidate)
    return urls


def path_parts(url: str) -> list[str]:
    """The URL's path segments. Parsed, not string-split: a foreign URL used to
    yield ['https:', 'example.com', ...] and file itself under a section called
    "https:" instead of being dropped."""
    return [p for p in urlsplit(url).path.split("/") if p]


def on_miso(url: str) -> bool:
    """misoenergy.org or a subdomain of it, over http(s)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return (parts.scheme in ("http", "https")
            and (host == SITE_HOST or host.endswith("." + SITE_HOST)))


def label(segment: str) -> str:
    """A URL path segment as something a person would read."""
    words = re.sub(r"\s+", " ", segment.replace("_", "-").replace("-", " ")).strip()
    return " ".join(ACRONYMS.get(w.lower(), w.capitalize()) for w in words.split())


def keep(url: str) -> bool:
    """True for pages worth sending someone to."""
    if not on_miso(url):
        return False
    parts = path_parts(url)
    if not parts or parts[0] in DROP_SECTIONS:
        return False
    if any(frag in url for frag in DROP_IF_CONTAINS):
        return False
    # keep the notifications index, drop the dated posts beneath it
    if "/notifications/" in url and len(parts) > 2:
        return False
    return True


def build(urls: list[str]) -> tuple[str, int]:
    """The index as markdown, plus how many pages it lists."""
    sections: dict[str, list[tuple[list[str], str]]] = defaultdict(list)
    for url in urls:
        if keep(url):
            sections[path_parts(url)[0]].append((path_parts(url), url))

    lines = [
        "# MISO site index - where to find things on misoenergy.org",
        "",
        "A map of MISO's public website: which page covers which topic, and its link.",
        "Use it to send someone to the right page on misoenergy.org.",
        "",
        f"Generated {datetime.date.today():%Y-%m-%d} by backend/rag/build_site_index.py "
        f"from a saved copy of {SITEMAP_URL} .",
        "",
        "Each entry is: page name - where it sits - URL.",
        "",
    ]
    count = 0
    for section in sorted(sections, key=lambda s: -len(sections[s])):
        lines += [f"## {SECTION_TITLES.get(section, label(section))}", ""]
        for parts, url in sorted(sections[section], key=lambda t: t[1]):
            name = label(parts[-1]) if len(parts) > 1 else label(parts[0])
            trail = " > ".join(label(p) for p in parts[:-1]) or "top level"
            lines.append(f"- **{name}** - {trail} - {url}")
            count += 1
        lines.append("")
    return "\n".join(lines) + "\n", count


def main() -> None:
    if not SITEMAP_PATH.exists():
        sys.exit(f"No sitemap export at {SITEMAP_PATH} - save "
                 f"{SITEMAP_URL} there first.")
    urls = read_sitemap()
    markdown, count = build(urls)
    OUT_PATH.write_text(markdown, encoding="utf-8")
    print(f"{len(urls)} URLs in {SITEMAP_PATH.name} -> {count} pages kept")
    print(f"Wrote {OUT_PATH} ({len(markdown) / 1024:.1f} KB)")
    print("Re-run `python -m backend.rag.ingest_docs` (with the backend stopped) "
          "to put it in Chroma.")


if __name__ == "__main__":
    main()
