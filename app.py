"""MISO Copilot - Streamlit chat UI (fallback frontend).

Talks to the FastAPI backend at MISO_COPILOT_BACKEND (default
http://localhost:8000). If the backend is down or unconfigured, shows a
graceful handoff to MISO's contact form instead of an answer.
"""

import json
import os
import re

import pandas as pd
import requests
import streamlit as st

# Matches the ```chart and ```map fenced JSON blocks the backend puts in answers.
CHART_BLOCK = re.compile(r"```(chart|map)\s*\n(.*?)```", re.DOTALL)

BACKEND_URL = os.getenv("MISO_COPILOT_BACKEND", "http://localhost:8000")
CONTACT_URL = "https://www.misoenergy.org/about/contact-us/"

SAMPLE_QUESTIONS = [
    "How much wind power is MISO generating right now?",
    "What's the current fuel mix?",
    "Where can I find historical LMP (price) data?",
    "How does the generator interconnection queue work?",
    "The RT LMP CSV had an MLC column - where is it in the Data Exchange API?",
]

st.set_page_config(page_title="MISO Copilot", layout="centered")

with st.sidebar:
    st.title("MISO Copilot")
    st.caption(
        "Ask anything about MISO's public data - grid conditions, market reports, "
        "processes, filings. Answers cite their source and state how fresh the "
        "data is."
    )
    st.divider()
    st.subheader("Try asking")
    for q in SAMPLE_QUESTIONS:
        if st.button(q, use_container_width=True):
            st.session_state.queued_question = q
    st.divider()
    st.caption(
        "Fall 2026 MISO Xtern Challenge - Prompt 1. Data: MISO public APIs & "
        "public documents. Not an official MISO product."
    )

st.title("MISO Copilot")
st.caption("The front door to MISO's public data. Ask in plain English.")


def render_map(spec_json: str) -> None:
    """The React widget draws a map; here a caption names the highlighted states."""
    try:
        cfg = json.loads(spec_json)
        states = ", ".join(str(s) for s in cfg["highlight"])
    except (KeyError, ValueError, TypeError):
        st.code(spec_json, language="json")
        return
    st.caption(f"Map: {cfg.get('title', 'MISO footprint')} - {states} "
               "(MISO serves all or part of each, plus Manitoba)")


def render_chart(spec_json: str) -> None:
    """Render one ```chart block; falls back to showing the raw JSON."""
    try:
        cfg = json.loads(spec_json)
        df = pd.DataFrame(
            {s["name"]: [float(v) for v in s["data"]] for s in cfg["series"]},
            index=[str(label) for label in cfg["labels"]],
        )
    except (KeyError, ValueError, TypeError):
        st.code(spec_json, language="json")
        return

    if cfg.get("title"):
        st.caption(cfg["title"])

    kind = cfg.get("type", "line")
    if kind == "area":
        st.area_chart(df)
    elif kind == "bar" or kind == "pie":
        # Streamlit has no native pie chart, so pie falls back to bars.
        st.bar_chart(df)
    else:
        st.line_chart(df)


def render_answer(markdown_text: str) -> None:
    """Render an answer: markdown (incl. $LaTeX$ and tables) via st.markdown,
    with ```chart blocks rendered as Streamlit charts in between."""
    pos = 0
    for match in CHART_BLOCK.finditer(markdown_text):
        text_before_chart = markdown_text[pos : match.start()]
        if text_before_chart.strip():
            st.markdown(text_before_chart)
        if match.group(1) == "map":
            render_map(match.group(2))
        else:
            render_chart(match.group(2))
        pos = match.end()

    text_after_last_chart = markdown_text[pos:]
    if text_after_last_chart.strip():
        st.markdown(text_after_last_chart)


def get_answer(question: str) -> str:
    """POST the question to the backend; return markdown-formatted answer."""
    try:
        resp = requests.post(
            f"{BACKEND_URL}/ask", json={"question": question}, timeout=60
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return (
            "I couldn't reach MISO Copilot's data service just now. For help "
            f"with this question, please use the [MISO Contact Form]({CONTACT_URL})."
        )

    parts = [data["answer"]]
    if data.get("as_of"):
        parts.append(f"*as of {data['as_of']}*")
    for s in data.get("sources", []):
        parts.append(f"Source: [{s.get('title', s['url'])}]({s['url']})")
    if "apim.misoenergy.org" in data["answer"]:   # a crosswalk answer
        parts.append(f"[Download the full crosswalk (CSV)]({BACKEND_URL}/crosswalk.csv)")
    return "\n\n".join(parts)


if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        render_answer(msg["content"])

question = st.chat_input("Ask about MISO's grid, markets, reports, or processes…")
if not question and "queued_question" in st.session_state:
    question = st.session_state.pop("queued_question")

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Searching MISO's public data…"):
            answer = get_answer(question)
        render_answer(answer)
    st.session_state.messages.append({"role": "assistant", "content": answer})
