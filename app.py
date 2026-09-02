"""MISO Copilot - Streamlit chat UI (fallback frontend).

Talks to the FastAPI backend at MISO_COPILOT_BACKEND (default
http://localhost:8000). If the backend is down or unconfigured, shows a
graceful handoff to MISO's contact form instead of an answer.
"""

import json
import os
import re

import requests
import streamlit as st

CHART_BLOCK = re.compile(r"```chart\s*\n(.*?)```", re.DOTALL)

BACKEND_URL = os.getenv("MISO_COPILOT_BACKEND", "http://localhost:8000")
CONTACT_URL = "https://www.misoenergy.org/about/contact-us/"

SAMPLE_QUESTIONS = [
    "How much wind power is MISO generating right now?",
    "What's the current fuel mix?",
    "Where can I find historical LMP (price) data?",
    "How does the generator interconnection queue work?",
    "What is MISO's latest seasonal reliability assessment?",
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


def render_answer(markdown_text: str) -> None:
    """Render an answer: markdown (incl. $LaTeX$ and tables) via st.markdown,
    and ```chart blocks as Streamlit charts."""
    import pandas as pd

    pos = 0
    for match in CHART_BLOCK.finditer(markdown_text):
        before = markdown_text[pos : match.start()]
        if before.strip():
            st.markdown(before)
        pos = match.end()
        try:
            cfg = json.loads(match.group(1))
            df = pd.DataFrame(
                {s["name"]: [float(v) for v in s["data"]] for s in cfg["series"]},
                index=[str(x) for x in cfg["labels"]],
            )
            if cfg.get("title"):
                st.caption(cfg["title"])
            kind = cfg.get("type", "line")
            if kind == "area":
                st.area_chart(df)
            elif kind in ("bar", "pie"):  # no native pie; bar is the fallback
                st.bar_chart(df)
            else:
                st.line_chart(df)
        except (KeyError, ValueError, TypeError):
            st.code(match.group(1), language="json")
    rest = markdown_text[pos:]
    if rest.strip():
        st.markdown(rest)


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
