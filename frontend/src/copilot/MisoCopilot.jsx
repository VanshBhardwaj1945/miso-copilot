import { useEffect, useRef, useState } from "react";
import Markdown from "./Markdown.jsx";
import "./MisoCopilot.css";

// Design + product rules: see UI_RULES.md.
// Backend: POST /ask {question} -> {answer, sources[{title,url}], as_of}
// (/ask is proxied to FastAPI on :8000, see vite.config.js)

const SUGGESTED_QUESTIONS = [
  "What's the current fuel mix?",
  "How much wind power is MISO generating right now?",
  "Where can I find historical LMP (price) data?",
  "How does the generator interconnection queue work?",
];

const CONTACT_URL = "https://www.misoenergy.org/about/contact-us/";

const DEFAULT_SIZE = { width: 360, height: 520 };
const MIN_SIZE = { width: 320, height: 380 };
const MAX_INPUT_HEIGHT = 120;

export default function MisoCopilot() {
  const [isOpen, setIsOpen] = useState(true);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [size, setSize] = useState(DEFAULT_SIZE);
  const bodyRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, loading]);

  // Panel is anchored bottom-right; dragging the top-left grip grows it
  // up and to the left.
  function startResize(e) {
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const { width, height } = size;

    const onMove = (ev) => {
      setSize({
        width: Math.min(
          Math.max(width + (startX - ev.clientX), MIN_SIZE.width),
          window.innerWidth - 48
        ),
        height: Math.min(
          Math.max(height + (startY - ev.clientY), MIN_SIZE.height),
          window.innerHeight - 48
        ),
      });
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function autoGrow() {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, MAX_INPUT_HEIGHT) + "px";
  }

  async function ask(question) {
    const q = question.trim();
    if (!q || loading) return;

    setMessages((m) => [...m, { role: "user", content: q }]);
    setInput("");
    if (inputRef.current) inputRef.current.style.height = "auto";
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

  const handleSubmit = (e) => {
    e.preventDefault();
    ask(input);
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ask(input);
    }
  };

  return (
    <>
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

            {messages.map((msg, i) => (
              <div
                key={i}
                className={
                  msg.role === "user"
                    ? "miso-msg miso-msg-user"
                    : "miso-msg miso-msg-assistant" +
                      (msg.error ? " miso-msg-error" : "")
                }
              >
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
                  </div>
                )}
              </div>
            ))}

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
