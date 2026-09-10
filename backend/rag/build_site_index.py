"""Build site_index.md - a map of which misoenergy.org page covers which topic.

The copilot's most common job is "where do I find X?", and for that the link is
the answer. This turns MISO's published sitemap into a short, readable index the
doc lane can retrieve from, so the answer carries a real URL instead of a guess.

Run:  python -m backend.rag.build_site_index

One request, for https://www.misoenergy.org/sitemap.xml - a file MISO publishes
for exactly this purpose. That is not crawling, and the URL list it returns is
NOT a license to fetch those pages: miso.org bans scrapers (see AGENTS.md).

Like crosswalk.json, the output is committed. It cannot be fetched per-file the
way doc_sources.json entries are, so it ships in the repo and teammates get it
from a clone rather than a build step they have to remember.
"""

import datetime
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import requests

SITEMAP_URL = "https://www.misoenergy.org/sitemap.xml"
OUT_PATH = Path(__file__).resolve().parent / "site_index.md"
TIMEOUT = 30

# Sections that are one-off content rather than places to send someone. Events
# and stakeholder engagement alone are 86 percent of the sitemap, and a meeting
# page from 2023 is never the answer to "where do I find X?".
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


def fetch_sitemap(url: str = SITEMAP_URL) -> list[str]:
    """Every <loc> in MISO's sitemap. One polite request."""
    resp = requests.get(url, timeout=TIMEOUT,
                        headers={"User-Agent": "miso-copilot/1.0 (site index builder)"})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    # the sitemap namespace is declared on the root; match on the tag's suffix
    return [el.text.strip() for el in root.iter()
            if el.tag.endswith("loc") and el.text]


def path_parts(url: str) -> list[str]:
    return [p for p in url.split("misoenergy.org/")[-1].split("/") if p]


def label(segment: str) -> str:
    """A URL path segment as something a person would read."""
    words = re.sub(r"\s+", " ", segment.replace("_", "-").replace("-", " ")).strip()
    return " ".join(ACRONYMS.get(w.lower(), w.capitalize()) for w in words.split())


def keep(url: str) -> bool:
    """True for pages worth sending someone to."""
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
        f"from {SITEMAP_URL} .",
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
    urls = fetch_sitemap()
    markdown, count = build(urls)
    OUT_PATH.write_text(markdown, encoding="utf-8")
    print(f"{len(urls)} URLs in the sitemap -> {count} pages kept")
    print(f"Wrote {OUT_PATH} ({len(markdown) / 1024:.1f} KB)")
    print("Re-run `python -m backend.rag.ingest_docs` (with the backend stopped) "
          "to put it in Chroma.")


if __name__ == "__main__":
    main()
