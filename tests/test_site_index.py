"""The site index: what gets kept, how it reads, and how it lands in Chroma.

No network. fetch_sitemap is driven with XML strings; everything else is pure.

The filter is the part worth protecting: 86 percent of MISO's sitemap is event
and engagement pages, and letting those back in would bury the 209 pages that
actually answer "where do I find X?".
"""

import pytest

from backend.rag import build_site_index as bsi

SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.misoenergy.org/</loc><lastmod>2025-10-08</lastmod></url>
  <url><loc>https://www.misoenergy.org/markets-and-operations/</loc></url>
  <url><loc>https://www.misoenergy.org/events/2023-meeting/</loc></url>
</urlset>"""


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


# --- fetch_sitemap -------------------------------------------------------

def test_fetch_sitemap_reads_loc_elements_through_the_namespace(monkeypatch):
    """The sitemap declares a default namespace, so <loc> is really {ns}loc."""
    monkeypatch.setattr(bsi.requests, "get", lambda *a, **k: FakeResponse(SITEMAP_XML))
    urls = bsi.fetch_sitemap()
    assert urls == [
        "https://www.misoenergy.org/",
        "https://www.misoenergy.org/markets-and-operations/",
        "https://www.misoenergy.org/events/2023-meeting/",
    ]


def test_fetch_sitemap_refuses_an_entity_expansion_bomb(monkeypatch):
    """defusedxml, not stdlib ElementTree - this parses XML off the network.

    Here because bandit B314 caught the stdlib version in review; the stdlib
    parser expands these happily.
    """
    bomb = b"""<?xml version="1.0"?>
    <!DOCTYPE urlset [
      <!ENTITY a "aaaaaaaaaa">
      <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
    ]>
    <urlset><url><loc>&b;</loc></url></urlset>"""
    monkeypatch.setattr(bsi.requests, "get", lambda *a, **k: FakeResponse(bomb))
    with pytest.raises(Exception) as err:
        bsi.fetch_sitemap()
    assert "entit" in str(err.value).lower() or "Entities" in str(err.value)


# --- keep ----------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://www.misoenergy.org/markets-and-operations/arr-ftr-market/",
    "https://www.misoenergy.org/planning/resource-utilization/",
    "https://www.misoenergy.org/legal/tariff/",
    "https://www.misoenergy.org/markets-and-operations/notifications/",
])
def test_keep_admits_navigational_pages(url):
    assert bsi.keep(url) is True


@pytest.mark.parametrize("url", [
    "https://www.misoenergy.org/events/2023-planning-meeting/",
    "https://www.misoenergy.org/engage/committees/",
    "https://www.misoenergy.org/meet-miso/media-center/2026---news-releases/miso-x/",
    "https://www.misoenergy.org/markets-and-operations/notifications/arrftr-notifications/2026-rsp-round-3/",
])
def test_keep_drops_one_off_content(url):
    assert bsi.keep(url) is False


@pytest.mark.parametrize("url", [
    "https://example.com/markets-and-operations/",
    "https://evil.com/?x=misoenergy.org/planning/",
    "javascript:alert(1)",
    "ftp://www.misoenergy.org/planning/",
])
def test_keep_rejects_urls_off_misoenergy(url):
    """Found in review: a substring match filed foreign URLs under a section
    literally named "https:" instead of dropping them."""
    assert bsi.keep(url) is False


@pytest.mark.parametrize("url", [
    "https://www.misoenergy.org/planning/",
    "https://docs.misoenergy.org/marketreports/guide/",
    "https://misoenergy.org/legal/tariff/",
])
def test_keep_admits_misoenergy_subdomains(url):
    """Readers' guides live on docs.misoenergy.org, so subdomains count."""
    assert bsi.keep(url) is True


def test_keep_drops_the_bare_domain():
    """No path segments means nothing to label or file under a section."""
    assert bsi.keep("https://www.misoenergy.org/") is False


def test_notifications_index_survives_but_its_dated_posts_do_not():
    """The index page is a real destination; each post underneath is news."""
    index = "https://www.misoenergy.org/markets-and-operations/notifications/"
    post = index + "arrftr-notifications/october-2026-mpma-model-now-available/"
    assert bsi.keep(index) is True
    assert bsi.keep(post) is False


# --- label ---------------------------------------------------------------

@pytest.mark.parametrize("segment,expected", [
    ("arr-ftr-market", "ARR FTR Market"),
    ("real-time--market-data", "Real Time Market Data"),
    ("grid_planning_basics", "Grid Planning Basics"),
    ("market-reports", "Market Reports"),
])
def test_label_reads_like_a_page_name(segment, expected):
    assert bsi.label(segment) == expected


def test_label_keeps_acronyms_upper_case():
    """A naive .capitalize() turns LMP into "Lmp", which reads as a typo."""
    assert bsi.label("historical-lmp") == "Historical LMP"
    assert bsi.label("ferc-filings") == "FERC Filings"


# --- build ---------------------------------------------------------------

URLS = [
    "https://www.misoenergy.org/markets-and-operations/",
    "https://www.misoenergy.org/markets-and-operations/arr-ftr-market/",
    "https://www.misoenergy.org/planning/generator-interconnection/",
    "https://www.misoenergy.org/events/some-meeting/",
]


def test_build_counts_only_kept_pages():
    _, count = bsi.build(URLS)
    assert count == 3


def test_build_emits_every_url_verbatim():
    """The URL is the product - a mangled one sends someone to a 404."""
    markdown, _ = bsi.build(URLS)
    assert "https://www.misoenergy.org/markets-and-operations/arr-ftr-market/" in markdown
    assert "events/some-meeting" not in markdown


def test_build_groups_under_readable_section_headings():
    markdown, _ = bsi.build(URLS)
    assert "## Markets and Operations - real-time data, market reports, pricing" in markdown
    assert "## Planning - generator interconnection, MTEP, resource adequacy" in markdown


def test_build_orders_sections_by_size():
    """The section someone is most likely to want comes first."""
    markdown, _ = bsi.build(URLS)
    assert markdown.index("## Markets and Operations") < markdown.index("## Planning")


def test_build_survives_a_sitemap_with_nothing_worth_keeping():
    markdown, count = bsi.build(["https://www.misoenergy.org/events/x/"])
    assert count == 0
    assert markdown.startswith("# MISO site index")


# --- ingest --------------------------------------------------------------

def test_ingest_site_index_is_a_no_op_when_the_file_is_absent(monkeypatch, tmp_path):
    """A clone that has not run the generator must still ingest the rest."""
    from backend.rag import ingest_docs
    monkeypatch.setattr(ingest_docs, "SITE_INDEX_PATH", tmp_path / "missing.md")

    def explode(*a, **k):
        raise AssertionError("must not touch the index when the file is missing")

    assert ingest_docs.ingest_site_index(explode, explode) == 0


class RecordingSplitter:
    def __init__(self):
        self.documents = None

    def get_nodes_from_documents(self, documents):
        self.documents = documents
        return ["node-a", "node-b"]


class RecordingIndex:
    def __init__(self):
        self.nodes = None

    def insert_nodes(self, nodes):
        self.nodes = nodes


def test_ingest_site_index_tags_chunks_reference_doc(monkeypatch, tmp_path):
    """The tag is load-bearing, not cosmetic.

    ingest_general_docs evicts by `doc_type == "reference_doc"` before
    inserting. A chunk tagged anything else would survive that sweep and be
    re-inserted every run, so the store would grow a duplicate copy of the
    whole index per ingest - silently, since nothing errors.
    """
    from backend.rag import ingest_docs
    index_file = tmp_path / "site_index.md"
    index_file.write_text("# MISO site index\n\n- **Market Reports** - x - https://y/\n")
    monkeypatch.setattr(ingest_docs, "SITE_INDEX_PATH", index_file)

    splitter, index = RecordingSplitter(), RecordingIndex()
    count = ingest_docs.ingest_site_index(index, splitter)

    assert count == 2
    assert index.nodes == ["node-a", "node-b"]
    doc = splitter.documents[0]
    assert doc.metadata["doc_type"] == "reference_doc"
    assert doc.metadata["source_url"]
    assert "Market Reports" in doc.text


def test_ingest_site_index_keeps_the_url_out_of_the_embedding():
    """source_url is metadata, not meaning - embedding it only adds noise."""
    from backend.rag import ingest_docs
    import inspect
    source = inspect.getsource(ingest_docs.ingest_site_index)
    assert "excluded_embed_metadata_keys" in source
    assert "excluded_llm_metadata_keys" in source
