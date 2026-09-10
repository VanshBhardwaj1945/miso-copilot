"""search_docs: two lanes, one seat each, and the citations that come out.

The lane split exists because of a real failure - one shared top-k let Fact
Sheet chunks crowd the live numbers out of "what are grid conditions?". These
tests hold that line, with a fake index so nothing touches Chroma.
"""

import pytest

from backend.rag import retriever


class Node:
    def __init__(self, text, doc_type, title=None, url=None, endpoint=None, as_of=None):
        self._text = text
        self.metadata = {"doc_type": doc_type}
        if title:
            self.metadata["title"] = title
        if url:
            self.metadata["source_url"] = url
        if endpoint:
            self.metadata["endpoint"] = endpoint
        if as_of:
            self.metadata["as_of"] = as_of

    def get_content(self):
        return self._text


class FakeIndex:
    """Records the top_k asked of each lane and returns that lane's nodes."""

    def __init__(self, by_lane):
        self.by_lane = by_lane
        self.asked = {}

    def as_retriever(self, similarity_top_k, filters):
        lane = filters.filters[0].value
        self.asked[lane] = similarity_top_k
        nodes = self.by_lane.get(lane, [])
        return _Retriever(nodes[:similarity_top_k])


class _Retriever:
    def __init__(self, nodes):
        self.nodes = nodes

    def retrieve(self, _query):
        return self.nodes


@pytest.fixture
def index(monkeypatch):
    def install(by_lane):
        fake = FakeIndex(by_lane)
        monkeypatch.setattr(retriever, "get_index", lambda: fake)
        return fake
    return install


LIVE = Node("Wind is 1,500 MW.", "live_snapshot", "WindSolar Display",
            "https://www.misoenergy.org/live", endpoint="WindSolar", as_of="09:25 EST")
DOC = Node("Market Reports hold historical LMPs.", "reference_doc",
           "Market Reports catalog", "https://www.misoenergy.org/reports")


# --- the lane split -------------------------------------------------------

def test_each_lane_is_searched_separately_with_its_own_budget(index):
    fake = index({"live_snapshot": [LIVE], "reference_doc": [DOC]})
    retriever.search_docs("grid conditions")
    assert fake.asked == {"live_snapshot": retriever.LIVE_TOP_K,
                          "reference_doc": retriever.DOC_TOP_K}


def test_live_snapshots_come_first(index):
    """Ordering is the contract with the prompt: the model reads the numbers
    before the reference prose.
    """
    index({"live_snapshot": [LIVE], "reference_doc": [DOC]})
    context, _, _ = retriever.search_docs("q")
    assert context.index("Wind is 1,500 MW.") < context.index("Market Reports")


def test_a_wordy_document_question_cannot_starve_the_live_lane(index):
    """The failure this design exists to prevent: many strong doc matches used
    to fill a single shared top-k and push the live numbers out entirely.
    """
    docs = [Node(f"doc {i}", "reference_doc", f"T{i}", f"https://x/{i}") for i in range(20)]
    index({"live_snapshot": [LIVE], "reference_doc": docs})
    context, _, _ = retriever.search_docs("what are grid conditions?")
    assert "Wind is 1,500 MW." in context


def test_a_caller_can_widen_only_the_document_lane(index):
    fake = index({"live_snapshot": [LIVE], "reference_doc": [DOC]})
    retriever.search_docs("q", top_k=9)
    assert fake.asked["reference_doc"] == 9
    assert fake.asked["live_snapshot"] == retriever.LIVE_TOP_K


# --- sources --------------------------------------------------------------

def test_each_source_is_returned_once(index):
    same = Node("chunk two of the same page", "reference_doc",
                "Market Reports catalog", "https://www.misoenergy.org/reports")
    index({"live_snapshot": [], "reference_doc": [DOC, same]})
    _, sources, _ = retriever.search_docs("q")
    assert sources == [{"title": "Market Reports catalog",
                        "url": "https://www.misoenergy.org/reports"}]


def test_a_chunk_with_no_url_is_used_but_not_cited(index):
    """Rule 6: a citation must be a real link, so a chunk without one
    contributes context and nothing else.
    """
    index({"live_snapshot": [], "reference_doc": [Node("useful text", "reference_doc")]})
    context, sources, _ = retriever.search_docs("q")
    assert "useful text" in context
    assert sources == []


def test_a_chunk_with_no_title_still_cites_its_link(index):
    index({"live_snapshot": [],
           "reference_doc": [Node("t", "reference_doc", url="https://x/y")]})
    _, sources, _ = retriever.search_docs("q")
    assert sources == [{"title": "MISO Resource", "url": "https://x/y"}]


# --- snapshots ------------------------------------------------------------

def test_only_one_snapshot_per_endpoint_reaches_the_prompt(index):
    """Two chunks of the same endpoint would be the same numbers twice."""
    older = Node("Wind was 1,400 MW.", "live_snapshot", "WindSolar Display",
                 "https://www.misoenergy.org/live", endpoint="WindSolar", as_of="09:20 EST")
    index({"live_snapshot": [LIVE, older], "reference_doc": []})
    context, _, _ = retriever.search_docs("q")
    assert "1,500" in context and "1,400" not in context


def test_different_endpoints_both_reach_the_prompt(index):
    other = Node("Load is 84,172 MW.", "live_snapshot", "Load Display",
                 "https://www.misoenergy.org/live", endpoint="RealTimeTotalLoad",
                 as_of="09:25 EST")
    index({"live_snapshot": [LIVE, other], "reference_doc": []})
    context, _, _ = retriever.search_docs("q")
    assert "1,500" in context and "84,172" in context


def test_the_freshest_as_of_is_reported(index):
    index({"live_snapshot": [LIVE], "reference_doc": [DOC]})
    _, _, as_of = retriever.search_docs("q")
    assert as_of == "09:25 EST"


def test_no_snapshots_means_no_as_of(index):
    """A pure document answer has no staleness to disclose."""
    index({"live_snapshot": [], "reference_doc": [DOC]})
    _, _, as_of = retriever.search_docs("q")
    assert as_of is None


def test_an_empty_store_returns_empty_context_rather_than_raising(index):
    index({"live_snapshot": [], "reference_doc": []})
    context, sources, as_of = retriever.search_docs("q")
    assert context == "" and sources == [] and as_of is None
