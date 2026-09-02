"""MISO Copilot — Streamlit chat UI.

Runs standalone for now: answers come from a stub until the FastAPI backend
(/ask endpoint) is wired in. Set BACKEND_URL when the backend exists.
"""

import os
from datetime import datetime

import streamlit as st

BACKEND_URL = os.getenv("MISO_COPILOT_BACKEND", "")  # e.g. http://localhost:8000

SAMPLE_QUESTIONS = [
    "How much wind power is MISO generating right now?",
    "What's the current fuel mix?",
    "Where can I find historical LMP (price) data?",
    "How does the generator interconnection queue work?",
    "What is MISO's latest seasonal reliability assessment?",
]

st.set_page_config(page_title="MISO Copilot", page_icon="⚡", layout="centered")

with st.sidebar:
    st.title("⚡ MISO Copilot")
    st.caption(
        "Ask anything about MISO's public data — grid conditions, market reports, "
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
        "Fall 2026 MISO Xtern Challenge — Prompt 1. Data: MISO public APIs & "
        "public documents. Not an official MISO product."
    )

st.title("MISO Copilot")
st.caption("The front door to MISO's public data. Ask in plain English.")


def get_answer(question: str) -> str:
    """Return an answer for the question.

    Stub until the FastAPI backend exists. Once it does, POST the question to
    f"{BACKEND_URL}/ask" and return the response text + sources.
    """
    if BACKEND_URL:
        import requests

        resp = requests.post(f"{BACKEND_URL}/ask", json={"question": question}, timeout=60)
        resp.raise_for_status()
        return resp.json()["answer"]

    now = datetime.now().strftime("%-I:%M %p")
    return (
        f"🚧 **Backend not wired up yet** — this is the UI skeleton.\n\n"
        f"When it's connected, I'll answer *\"{question}\"* from MISO's public "
        f"data and reply with something like:\n\n"
        f"> As of {now} EST, MISO's total generation is 114,136 MW. Natural gas "
        f"leads with 44,591 MW (39%), followed by coal at 37,817 MW (33%), "
        f"nuclear at 11,871 MW, and wind at 5,639 MW.\n>\n"
        f"> Source: [MISO Real-Time Displays]"
        f"(https://www.misoenergy.org/markets-and-operations/real-time--market-data/real-time-displays/)"
    )


if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

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
        st.markdown(answer)
    st.session_state.messages.append({"role": "assistant", "content": answer})
