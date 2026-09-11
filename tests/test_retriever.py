"""search_docs: three lanes, each with its own budget, and the citations.

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
SETTLED = Node("MISO actual load by region for the completed market day.",
               "settled_market_day", "MISO Data Exchange - actual load",
               "https://data-exchange.misoenergy.org/x",
               endpoint="DEActualLoad", as_of="2026-09-09T23:00:00")


# --- the lane split -------------------------------------------------------

def test_each_lane_is_searched_separately_with_its_own_budget(index):
    fake = index({"live_snapshot": [LIVE], "settled_market_day": [SETTLED],
                  "reference_doc": [DOC]})
    retriever.search_docs("grid conditions")
    # literals, not the constants: asserting a constant against itself passes
    # for any value it is given, so widening a budget back into the crowding
    # bug would not fail here
    assert fake.asked == {"live_snapshot": 3, "settled_market_day": 3,
                          "reference_doc": 4}


def test_the_live_lane_can_seat_a_whole_grid_question(index):
    """"What share of load is wind serving right now" needs the wind feed, the
    load feed and the fuel mix at once. Two seats cut one of the three, and
    the entire live corpus is four short paragraphs."""
    live = [Node(f"live {i}", "live_snapshot", f"L{i}", f"https://l/{i}",
                 endpoint=f"L{i}", as_of="09:25 EST") for i in range(4)]
    index({"live_snapshot": live, "settled_market_day": [], "reference_doc": []})
    context, _, _ = retriever.search_docs("what share of load is wind serving right now")
    assert context.count("live ") >= 3


def test_a_settled_market_day_cannot_crowd_out_the_live_feeds(index):
    """The regression this lane exists to prevent. Eleven settled documents
    share an opening sentence, embed as a near-duplicate block, and took every
    seat on "total generation right now" - answering from yesterday while the
    live feed that knew the answer was cut.
    """
    settled = [Node(f"settled {i}", "settled_market_day", f"DE{i}",
                    f"https://x/{i}", endpoint=f"DE{i}", as_of="2026-09-09T23:00:00")
               for i in range(11)]
    index({"live_snapshot": [LIVE], "settled_market_day": settled, "reference_doc": []})
    context, _, as_of = retriever.search_docs("total generation right now")
    assert "Wind is 1,500 MW." in context
    # and the answer is stamped with the live time, not yesterday's
    assert as_of == "09:25 EST"


def test_a_settled_as_of_never_stamps_the_answer(index):
    """Stamping an answer with yesterday says the whole answer is a day old,
    even when the live feeds supplied it."""
    settled = Node("settled", "settled_market_day", "DE", "https://x",
                   endpoint="DE", as_of="2026-09-09T23:00:00")
    index({"live_snapshot": [], "settled_market_day": [settled], "reference_doc": []})
    _, _, as_of = retriever.search_docs("q")
    assert as_of is None


def test_one_document_per_endpoint_in_the_settled_lane_too(index):
    older = Node("older", "settled_market_day", "DE", "https://x", endpoint="DEFuelMix")
    newer = Node("newer", "settled_market_day", "DE", "https://x", endpoint="DEFuelMix")
    index({"live_snapshot": [], "settled_market_day": [newer, older],
           "reference_doc": []})
    context, _, _ = retriever.search_docs("q")
    assert "newer" in context and "older" not in context


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


# --- which live stamp reaches the answer ----------------------------------

def test_the_answer_is_stamped_with_the_freshest_live_feed_not_the_first(index):
    """The lanes come back in score order, so a question that ranked WindSolar
    above the fuel mix was stamped 19:00 while the answer body quoted 19:45 -
    the header contradicting the paragraph directly under it. Measured on 7 of
    43 real questions."""
    older = Node("wind", "live_snapshot", "WindSolar", "https://w",
                 endpoint="WindSolar", as_of="10-Sep-2026 - Interval 19:00 EST")
    newer = Node("mix", "live_snapshot", "FuelMix", "https://f",
                 endpoint="FuelMix", as_of="10-Sep-2026 - Interval 19:45 EST")
    index({"live_snapshot": [older, newer], "settled_market_day": [],
           "reference_doc": []})
    assert retriever.search_docs("q")[2] == "10-Sep-2026 - Interval 19:45 EST"


def test_the_two_live_stamp_formats_are_compared_not_sorted_as_text(index):
    """The Snapshot stamps a clock time and the other three an interval. As
    strings "9/10/2026..." sorts after "10-Sep-2026..." whatever the hour."""
    interval = Node("mix", "live_snapshot", "FuelMix", "https://f",
                    endpoint="FuelMix", as_of="10-Sep-2026 - Interval 19:00 EST")
    clock = Node("snap", "live_snapshot", "Snapshot", "https://s",
                 endpoint="Snapshot", as_of="9/10/2026 8:25:00 PM EST")
    index({"live_snapshot": [interval, clock], "settled_market_day": [],
           "reference_doc": []})
    assert retriever.search_docs("q")[2] == "9/10/2026 8:25:00 PM EST"


def test_an_unrecognized_stamp_still_reaches_the_answer(index):
    """MISO drops RefId occasionally and the transformer says "Recent
    Interval". That is worth showing; dropping it would be a blank stamp."""
    odd = Node("x", "live_snapshot", "FuelMix", "https://f",
               endpoint="FuelMix", as_of="Recent Interval")
    index({"live_snapshot": [odd], "settled_market_day": [], "reference_doc": []})
    assert retriever.search_docs("q")[2] == "Recent Interval"
