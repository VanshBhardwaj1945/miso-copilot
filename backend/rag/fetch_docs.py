"""Download the reference-document corpus into data/docs/, politely.

Run:  python -m backend.rag.fetch_docs          (skips files already present)
      python -m backend.rag.fetch_docs --force  (re-downloads everything)

doc_sources.json lists each document: the local filename, the URL we cite in
answers, and (when different) the URL we download from. PDFs are saved as-is;
HTML pages are converted to a small markdown file. data/docs/ is gitignored -
MISO's files never enter the repo, only this script and the list do.

This is a one-time, hand-curated fetch: nine URLs, a browser User-Agent, a
pause between requests, no link-following. That is the "handful of polite
downloads" the mentors blessed, not scraping.
"""

import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SOURCES_PATH = HERE / "doc_sources.json"
DOCS_DIR = HERE.parent.parent / "data" / "docs"

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36")
PAUSE_SECONDS = 3
TIMEOUT_SECONDS = 30


def load_sources() -> list[dict]:
    """The curated document list, with `fetch` defaulting to the cited URL."""
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    for src in sources:
        src.setdefault("fetch", src["url"])
    return sources


class _PageToMarkdown(HTMLParser):
    """Keeps headings, paragraphs, and list links from a misoenergy.org page.

    Site chrome (header, nav, footer, scripts) is skipped wholesale. Each list
    item is prefixed with the heading it sits under, so a chunk that lands in
    the middle of a long list still says which category it belongs to.
    """

    SKIP = {"header", "nav", "footer", "script", "style", "noscript"}
    HEADINGS = {"h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._skip_depth = 0
        self._heading = ""
        self._buf: list[str] = []
        self._in: str | None = None      # "heading" | "p" | "li"
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.HEADINGS:
            self._in, self._buf = "heading", []
        elif tag == "p":
            self._in, self._buf = "p", []
        elif tag == "li":
            self._in, self._buf = "li", []
        elif tag == "a" and self._in:
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "a" and self._href is not None:
            text = _clean("".join(self._link_text))
            if text and self._in == "heading":
                self._buf.append(text)      # accordion titles are links; keep the words only
            elif text:
                self._buf.append(f"[{text}]({self._href})")
            self._href = None
        elif tag in self.HEADINGS and self._in == "heading":
            self._flush_heading()
        elif tag in ("p", "li") and self._in == tag:
            self._flush_block()

    def handle_data(self, data):
        if self._skip_depth or not self._in:
            return
        if self._href is not None:
            self._link_text.append(data)
        else:
            self._buf.append(data)

    def _flush_heading(self):
        text = _clean("".join(self._buf))
        if text:
            self._heading = text
            self.lines.append(f"\n## {text}\n")
        self._in = None

    def _flush_block(self):
        text = _clean("".join(self._buf))
        if text:
            prefix = f"{self._heading}: " if self._in == "li" and self._heading else ""
            self.lines.append(f"- {prefix}{text}" if self._in == "li" else text)
        self._in = None


def _clean(text: str) -> str:
    """Collapse the run-on whitespace that Word-pasted HTML is full of."""
    return re.sub(r"\s+", " ", text).strip()


def html_to_markdown(html: str, title: str, source_url: str) -> str:
    """A page's content as markdown, headed by its title and cited URL."""
    parser = _PageToMarkdown()
    parser.feed(html)
    body = "\n".join(parser.lines).strip()
    return f"# {title}\n\nSource: {source_url}\n\n{body}\n"


def fetch_one(src: dict, force: bool = False) -> str:
    """Download one source into data/docs/. Returns a one-word outcome."""
    target = DOCS_DIR / src["file"]
    if target.exists() and target.stat().st_size > 0 and not force:
        return "kept"

    response = requests.get(src["fetch"], headers={"User-Agent": USER_AGENT},
                            timeout=TIMEOUT_SECONDS)
    if response.status_code != 200:
        return f"http {response.status_code}"

    if target.suffix == ".pdf":
        # A rotated CDN filename returns an AccessDenied XML with a 200 - the
        # magic bytes are the only honest check.
        if not response.content.startswith(b"%PDF"):
            return "not a pdf"
        target.write_bytes(response.content)
    else:
        markdown = html_to_markdown(response.text, src["title"], src["url"])
        if markdown.count("\n") < 10:
            return "page parsed empty"
        target.write_text(markdown, encoding="utf-8")
    return "downloaded"


def fetch_all(force: bool = False) -> dict[str, str]:
    """Fetch every source, pausing between network requests."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    outcomes = {}
    for src in load_sources():
        outcome = fetch_one(src, force)
        outcomes[src["file"]] = outcome
        print(f"{outcome:>16}  {src['file']}")
        if outcome != "kept":
            time.sleep(PAUSE_SECONDS)
    return outcomes


if __name__ == "__main__":
    results = fetch_all(force="--force" in sys.argv)
    bad = [f for f, o in results.items() if o not in ("kept", "downloaded")]
    print(f"\n{len(results) - len(bad)} of {len(results)} documents ready in {DOCS_DIR}")
    sys.exit(1 if bad else 0)
