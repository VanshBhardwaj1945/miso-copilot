"""backend/llm/claude.py - retrieval, the Claude call, and the refusal path.

Never calls Anthropic. search_docs and the client are both replaced, so what is
tested is the wiring: that retrieved context reaches the prompt, that sources
come from Chroma metadata rather than from the model, and that a refusal or a
truncation is handled rather than passed through as an answer.
"""

import types

import pytest

from backend.llm import claude


class Block:
    def __init__(self, text, type="text"):
        self.text = text
        self.type = type


class Response:
    def __init__(self, blocks, stop_reason="end_turn"):
        self.content = blocks
        self.stop_reason = stop_reason


class FakeClient:
    """Stands in for anthropic.Anthropic, recording what it was asked."""

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.beta = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


SOURCES = [{"title": "MISO Fuel Mix", "url": "https://www.misoenergy.org/x"}]


@pytest.fixture
def retrieval(monkeypatch):
    """search_docs returns context and sources; tests override per case."""
    def set_result(context="Wind is 1,500 MW as of 09:25 EST.", sources=SOURCES):
        monkeypatch.setattr(claude, "search_docs",
                            lambda q: (context, sources, "09:25 EST"))
    set_result()
    return set_result


@pytest.fixture
def answering(monkeypatch, retrieval):
    def use(response):
        fake = FakeClient(response)
        monkeypatch.setattr(claude, "client", fake)
        monkeypatch.setattr(claude, "FORCE_MOCK", False)
        return fake
    return use


# --- the live path --------------------------------------------------------

def test_the_answer_is_the_models_text(answering):
    answering(Response([Block("Wind is **1,500 MW**.")]))
    answer, _, _ = claude.answer_question("how much wind?")
    assert answer == "Wind is **1,500 MW**."


def test_retrieved_context_reaches_the_prompt(answering):
    fake = answering(Response([Block("ok")]))
    claude.answer_question("how much wind?")
    sent = fake.calls[0]["messages"][0]["content"]
    assert "Wind is 1,500 MW as of 09:25 EST." in sent
    assert "how much wind?" in sent


def test_sources_come_from_chroma_not_from_the_model(answering):
    """The model never chooses a citation - metadata does, so a hallucinated
    link cannot reach the chips.
    """
    answering(Response([Block("some answer naming https://evil.example")]))
    _, sources, _ = claude.answer_question("q")
    assert sources == SOURCES


def test_several_text_blocks_are_joined(answering):
    answering(Response([Block("one "), Block("two")]))
    assert claude.answer_question("q")[0] == "one two"


def test_non_text_blocks_are_skipped(answering):
    answering(Response([Block("visible"), Block("hidden", type="thinking")]))
    assert claude.answer_question("q")[0] == "visible"


def test_a_question_with_no_context_is_sent_bare(answering, retrieval):
    retrieval(context="", sources=[])
    fake = answering(Response([Block("ok")]))
    claude.answer_question("who runs MISO?")
    assert fake.calls[0]["messages"][0]["content"] == "who runs MISO?"


def test_with_no_sources_the_answer_still_cites_miso(answering, retrieval):
    """Architecture rule 6: every doc-lane answer carries a link."""
    retrieval(sources=[])
    answering(Response([Block("ok")]))
    _, sources, _ = claude.answer_question("q")
    assert sources and sources[0]["url"].startswith("https://www.misoenergy.org")


# --- refusal and truncation ----------------------------------------------

def test_a_refusal_hands_off_to_miso_rather_than_answering(answering):
    answering(Response([Block("...")], stop_reason="refusal"))
    answer, sources, _ = claude.answer_question("something out of bounds")
    assert "reach out to MISO" in answer
    assert sources[0]["url"].endswith("/contact-us/")


def test_a_truncated_answer_says_so(answering):
    """Silently returning half a crosswalk table is worse than a short answer
    that admits it stopped.
    """
    answering(Response([Block("a long table...")], stop_reason="max_tokens"))
    answer, _, _ = claude.answer_question("q")
    assert "cut short" in answer


# --- mock mode ------------------------------------------------------------

def test_without_a_client_the_retrieved_context_is_returned_verbatim(monkeypatch, retrieval):
    monkeypatch.setattr(claude, "client", None)
    answer, sources, _ = claude.answer_question("q")
    assert "Mock Mode" in answer
    assert "Wind is 1,500 MW as of 09:25 EST." in answer
    assert sources == SOURCES


def test_mock_mode_says_so_when_nothing_was_retrieved(monkeypatch, retrieval):
    monkeypatch.setattr(claude, "client", None)
    retrieval(context="", sources=[])
    answer, sources, _ = claude.answer_question("q")
    assert "No relevant documents" in answer
    assert sources[0]["url"].startswith("https://www.misoenergy.org")


def test_force_mock_short_circuits_even_with_a_client(monkeypatch, answering):
    fake = answering(Response([Block("should not be used")]))
    monkeypatch.setattr(claude, "FORCE_MOCK", True)
    answer, _, _ = claude.answer_question("q")
    assert "Mock Mode" in answer
    assert fake.calls == []


# --- request shape --------------------------------------------------------

def test_the_request_carries_the_system_prompt_and_a_token_ceiling(answering):
    fake = answering(Response([Block("ok")]))
    claude.answer_question("q")
    call = fake.calls[0]
    assert call["system"]
    assert call["max_tokens"] >= 1000
    assert call["model"]


# --- the "as of" the retriever worked out ---------------------------------

def test_the_retrievers_as_of_is_carried_out_to_the_caller(answering):
    """The seam nobody owned. The route's half is covered by test_routes, but
    it monkeypatches answer_question - so returning None here left the route
    falling back to the wall clock and stamping yesterday's settled numbers
    with the current time, which is the bug this was all meant to fix.

    The 13 call sites above were widened from two-tuple to three-tuple with
    `_` in the new slot when the signature changed. That is a compile fix, not
    a test: every one of them passes with this value replaced by None.
    """
    answering(Response([Block("ok")]))
    assert claude.answer_question("q")[2] == "09:25 EST"


def test_mock_mode_carries_the_as_of_too(monkeypatch, retrieval):
    """Mock mode is the demo's fallback when Claude is unreachable, so it is
    exactly when a stale stamp would go unnoticed."""
    monkeypatch.setattr(claude, "client", None)
    assert claude.answer_question("q")[2] == "09:25 EST"


def test_a_refusal_still_reports_when_the_data_was_current(answering):
    answering(Response([Block("no")], stop_reason="refusal"))
    assert claude.answer_question("q")[2] == "09:25 EST"
