"""build_gas_notices: turning a third-party CSV into corpus text safely.

This export is republished from a couple of dozen pipeline operators' systems
and it does arrive damaged - one live row carries a raw 0xAC byte where an
ampersand belongs. So the free-text fields are treated as hostile.

Most of what is protected here was a real defect found in review: a Subject
could forge a clickable link, a `)` in a URL could close the markdown link
early and smuggle a second one, and a newline could forge a `##` heading into a
document the assistant cites as MISO's.
"""

from datetime import datetime, timedelta

import pytest

from backend.rag import build_gas_notices as bgn

NOW = datetime(2026, 9, 10, 12, 0)


def row(**over):
    base = {
        "Pipeline": "ANR", "Type": "Maintenance", "Subject": "Planned outage",
        "Posted (EST)": NOW.strftime("%m/%d/%Y %I:%M %p"),
        "Effective (EST)": "", "End (EST)": "",
        "Notice Url": "https://ebb.tceconnects.com/infopost/x?a=1&b=2",
    }
    base.update(over)
    return base


def bullets(markdown):
    return [ln for ln in markdown.splitlines() if ln.startswith("- **")]


def headings(markdown):
    return [ln for ln in markdown.splitlines() if ln.startswith("## ")]


# --- link trust ----------------------------------------------------------

def test_a_subject_cannot_forge_a_link():
    """Found in review: "[click here](javascript:alert(1))" in a Subject
    rendered as a real, clickable link in a cited document."""
    md, _, _ = bgn.build([row(Subject="Reduction [click here](javascript:alert(1))")], NOW)
    line = bullets(md)[0]
    assert "[click here]" not in line     # brackets neutralized, so no link forms
    assert "](javascript:" not in md


def test_a_closing_paren_in_a_url_cannot_smuggle_a_second_link():
    """Found in review: the `)` closed [notice](...) early and everything after
    it became a second link the reader never sees coming."""
    md, _, _ = bgn.build(
        [row(**{"Notice Url": "https://evil.com/a)[phish](https://phish.com"})], NOW)
    assert "phish" not in md
    assert md.count("[notice](") == 0     # unusable URL means no link at all


def test_a_newline_in_a_url_cannot_forge_a_heading():
    md, _, _ = bgn.build(
        [row(**{"Notice Url": "https://ok.com/x\n## Injected\nmalicious"})], NOW)
    assert "## Injected" not in md
    assert headings(md) == ["## ANR"]


def test_a_newline_in_a_pipeline_name_cannot_forge_a_heading():
    md, _, _ = bgn.build([row(Pipeline="ANR\n## Injected")], NOW)
    assert "## Injected" not in md
    assert len(headings(md)) == 1


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,<script>", "//evil.com/x",
    "http:evil", "ftp://ok.com/x", "", "   ", "https://ok.com/a b",
])
def test_only_plain_http_links_survive(url):
    assert bgn.safe_url(url) is None


@pytest.mark.parametrize("url", [
    "https://ebb.tceconnects.com/infopost/x?a=1&b=2",
    "http://pipeline2.kindermorgan.com/notice?id=7",
])
def test_real_operator_links_are_kept(url):
    """The link is the point - over-filtering would gut the document."""
    assert bgn.safe_url(url) == url


def test_a_real_notice_keeps_its_link():
    md, _, _ = bgn.build([row()], NOW)
    assert "[notice](https://ebb.tceconnects.com/infopost/x?a=1&b=2)" in md


# --- selection -----------------------------------------------------------

def test_the_cutoff_is_inclusive_at_the_boundary():
    old = (NOW - timedelta(days=bgn.RECENT_DAYS)).strftime("%m/%d/%Y %I:%M %p")
    older = (NOW - timedelta(days=bgn.RECENT_DAYS, minutes=1)).strftime("%m/%d/%Y %I:%M %p")
    _, kept_edge, _ = bgn.build([row(**{"Posted (EST)": old})], NOW)
    _, kept_past, _ = bgn.build([row(**{"Posted (EST)": older})], NOW)
    assert (kept_edge, kept_past) == (1, 0)


def test_the_cap_keeps_the_newest():
    rows = [row(Subject=f"notice {i}",
                **{"Posted (EST)": (NOW - timedelta(days=i)).strftime("%m/%d/%Y %I:%M %p")})
            for i in range(20)]
    md, kept, _ = bgn.build(rows, NOW)
    assert kept == bgn.MAX_PER_PIPELINE
    assert "notice 0" in md and f"notice {bgn.MAX_PER_PIPELINE}" not in md


def test_repeats_do_not_eat_the_cap():
    """Found in review: ANR's twelve slots held one notice three times, so a
    quarter of that pipeline's budget restated the same thing."""
    rows = [row(Subject="UPDATED: CAPACITY REDUCTION Southwest Area") for _ in range(5)]
    rows += [row(Subject=f"distinct {i}") for i in range(5)]
    md, kept, _ = bgn.build(rows, NOW)
    assert md.count("UPDATED: CAPACITY REDUCTION Southwest Area") == 1
    assert kept == 6


def test_undated_and_unparseable_rows_are_dropped_not_crashed():
    rows = [row(**{"Posted (EST)": ""}), row(**{"Posted (EST)": "not a date"}),
            row(**{"Posted (EST)": "2026-09-10 13:00"})]
    _, kept, _ = bgn.build(rows, NOW)
    assert kept == 0


def test_a_blank_pipeline_is_grouped_rather_than_lost():
    md, kept, _ = bgn.build([row(Pipeline=""), row(Pipeline="   ")], NOW)
    assert "## Unidentified pipeline" in md
    assert kept == 1     # both are the same notice, so dedupe folds them


# --- shape ---------------------------------------------------------------

def test_an_end_without_an_effective_still_appears():
    """It used to vanish: the window was only built when Effective was set."""
    md, _, _ = bgn.build([row(**{"End (EST)": "09/20/2026 05:00 AM"})], NOW)
    assert "until 20 Sep 2026" in md


def test_a_runaway_subject_is_truncated():
    md, _, _ = bgn.build([row(Subject="x" * 5000)], NOW)
    assert len(bullets(md)[0]) < 500


def test_an_empty_export_is_a_valid_document():
    md, kept, pipes = bgn.build([], NOW)
    assert (kept, pipes) == (0, 0)
    assert md.startswith("# MISO gas pipeline notices")


def test_output_is_deterministic():
    rows = [row(Subject=f"n{i}") for i in range(5)]
    assert bgn.build(rows, NOW)[0] == bgn.build(rows, NOW)[0]


def test_the_bom_does_not_swallow_the_first_column(tmp_path):
    """utf-8-sig: without it the first column becomes '\\ufeffPipeline' and
    every Pipeline value silently reads as blank."""
    csv_path = tmp_path / "g.csv"
    csv_path.write_text('﻿"Pipeline","Type"\n"ANR","Maintenance"\n', encoding="utf-8")
    assert bgn.load(csv_path)[0]["Pipeline"] == "ANR"
