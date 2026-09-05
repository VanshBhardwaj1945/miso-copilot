import { useEffect, useRef, useState } from "react";
import Markdown from "./Markdown.jsx";
import "./MisoCopilot.css";

// The Copilot chat panel. Design + product rules: see UI_RULES.md.
// Backend contract: POST /ask {question} -> {answer, sources[{title,url}], as_of}
// (/ask is proxied to FastAPI on :8000, see vite.config.js)

const SUGGESTED_QUESTIONS = [
  "How much wind power is MISO generating right now?",
  "Where can I find historical LMP (price) data?",
  "How does the generator interconnection queue work?",
  "Where did the LMP report's MLC column go in the new API?",
];

const CONTACT_URL = "https://www.misoenergy.org/about/contact-us/";

// Crosswalk answers always name the API host; that is how we know to offer the CSV.
const CROSSWALK_HINT = /apim\.misoenergy\.org/;

const DEFAULT_SIZE = { width: 360, height: 520 };
const MIN_SIZE = { width: 320, height: 380 };
const SCREEN_MARGIN = 48; // panel never gets closer than this to screen edges
const MAX_INPUT_HEIGHT = 120; // keep in sync with textarea max-height in the CSS

// Pick the CSS classes for one chat message bubble.
function messageClass(msg) {
  if (msg.role === "user") {
    return "miso-msg miso-msg-user";
  }
  if (msg.error) {
    return "miso-msg miso-msg-assistant miso-msg-error";
  }
  return "miso-msg miso-msg-assistant";
}

export default function MisoCopilot() {
  const [isOpen, setIsOpen] = useState(true);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [size, setSize] = useState(DEFAULT_SIZE);
  const bodyRef = useRef(null);
  const inputRef = useRef(null);

  // Keep the newest message scrolled into view.
  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, loading]);

  // Drag handler for the top-left grip: the panel is anchored bottom-right,
  // so moving the pointer up/left grows it.
  function startResize(e) {
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const startWidth = size.width;
    const startHeight = size.height;

    const onMove = (ev) => {
      const newWidth = startWidth + (startX - ev.clientX);
      const newHeight = startHeight + (startY - ev.clientY);
      setSize({
        width: clamp(newWidth, MIN_SIZE.width, window.innerWidth - SCREEN_MARGIN),
        height: clamp(newHeight, MIN_SIZE.height, window.innerHeight - SCREEN_MARGIN),
      });
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  // Keep value between min and max.
  function clamp(value, min, max) {
    if (value < min) return min;
    if (value > max) return max;
    return value;
  }

  // Grow the textarea to fit its content, up to MAX_INPUT_HEIGHT.
  function autoGrow() {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, MAX_INPUT_HEIGHT) + "px";
  }

  // Send a question to the backend and append both sides of the exchange.
  async function ask(question) {
    const q = question.trim();
    if (!q || loading) return;

    setMessages((m) => [...m, { role: "user", content: q }]);
    setInput("");
    if (inputRef.current) inputRef.current.style.height = "auto"; // shrink back after send
    setLoading(true);

    try {
      const resp = await fetch("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
      });
      if (!resp.ok) throw new Error(`Backend returned ${resp.status}`);
      const data = await resp.json();
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: data.answer,
          sources: data.sources || [],
          asOf: data.as_of || null,
        },
      ]);
    } catch (err) {
      // Graceful handoff - never a dead end (product rule, see UI_RULES.md).
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          error: true,
          content:
            "I couldn't reach MISO Copilot's data service just now. " +
            "For help with this question, please reach out to MISO directly.",
          sources: [{ title: "MISO Contact Form", url: CONTACT_URL }],
        },
      ]);
    } finally {
      setLoading(false);
    }
  }

  // Send button / form submit.
  const handleSubmit = (e) => {
    e.preventDefault();
    ask(input);
  };

  // Enter sends; Shift+Enter makes a new line.
  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ask(input);
    }
  };

  return (
    <>
      {/* Floating launcher, shown only while the panel is closed. */}
      {!isOpen && (
        <button
          className="miso-copilot-launcher"
          onClick={() => setIsOpen(true)}
          aria-label="Open MISO Copilot"
        >
          <span className="miso-copilot-sparkle" aria-hidden="true">✦</span>
        </button>
      )}

      {isOpen && (
        <aside
          className="miso-copilot"
          aria-label="MISO Copilot"
          style={{ width: size.width, height: size.height }}
        >
          <div
            className="miso-copilot-resize"
            onPointerDown={startResize}
            role="separator"
            aria-label="Resize MISO Copilot"
          />

          <header className="miso-copilot-header">
            <div className="miso-copilot-title">
              <div className="miso-copilot-icon" aria-hidden="true">
                <span>✦</span>
              </div>
              <div>
                <h2>MISO Copilot</h2>
                <span className="miso-copilot-status">
                  MISO Information Assistant
                </span>
              </div>
            </div>
            <button
              className="miso-copilot-close"
              onClick={() => setIsOpen(false)}
              aria-label="Close MISO Copilot"
            >
              ×
            </button>
          </header>

          <main className="miso-copilot-body" ref={bodyRef}>
            {/* Intro + suggested questions, shown until the first message. */}
            {messages.length === 0 && (
              <>
                <div className="miso-copilot-intro">
                  <h3>Hi! I'm MISO Copilot.</h3>
                  <p>
                    Ask me about the grid right now, market reports, MISO
                    processes, or filings — in plain English. Answers cite
                    their source and say how fresh the data is.
                  </p>
                </div>

                <div className="miso-suggestions">
                  {SUGGESTED_QUESTIONS.map((question) => (
                    <button
                      key={question}
                      className="miso-suggestion"
                      onClick={() => ask(question)}
                    >
                      <span className="miso-suggestion-icon" aria-hidden="true">
                        ▧
                      </span>
                      <span className="miso-suggestion-text">{question}</span>
                      <span className="miso-suggestion-arrow" aria-hidden="true">
                        ›
                      </span>
                    </button>
                  ))}
                </div>
              </>
            )}

            {/* Chat history. Assistant answers render markdown/math/charts;
                user messages stay plain text. */}
            {messages.map((msg, i) => (
              <div key={i} className={messageClass(msg)}>
                <div className="miso-msg-content">
                  {msg.role === "user" ? (
                    msg.content
                  ) : (
                    <Markdown>{msg.content}</Markdown>
                  )}
                </div>

                {msg.asOf && (
                  <div className="miso-msg-asof">as of {msg.asOf}</div>
                )}

                {msg.sources && msg.sources.length > 0 && (
                  <div className="miso-msg-sources">
                    {msg.sources.map((s) => (
                      <a
                        key={s.url}
                        href={s.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="miso-source-chip"
                      >
                        ↗ {s.title || s.url}
                      </a>
                    ))}
                    {msg.role === "assistant" && CROSSWALK_HINT.test(msg.content || "") && (
                      <a href="/crosswalk.csv" className="miso-source-chip miso-download-chip">
                        ⬇ Download the full crosswalk (CSV)
                      </a>
                    )}
                  </div>
                )}
              </div>
            ))}

            {/* Typing indicator while waiting on the backend. */}
            {loading && (
              <div
                className="miso-msg miso-msg-assistant miso-msg-loading"
                aria-label="MISO Copilot is searching"
              >
                <span className="miso-dot" />
                <span className="miso-dot" />
                <span className="miso-dot" />
              </div>
            )}
          </main>

          <form className="miso-copilot-input-wrapper" onSubmit={handleSubmit}>
            <textarea
              ref={inputRef}
              rows={1}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                autoGrow();
              }}
              onKeyDown={handleKeyDown}
              placeholder="Ask a question..."
              aria-label="Ask MISO Copilot a question"
              disabled={loading}
            />
            <button
              type="submit"
              className="miso-copilot-send"
              aria-label="Send question"
              disabled={loading}
            >
              →
            </button>
          </form>
        </aside>
      )}
    </>
  );
}
