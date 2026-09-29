import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  downloadUrl,
  sendFeedback,
  streamChat,
  versions,
  type Answer,
  type Candidate,
  type Source,
  type VersionInfo,
} from "./api";
import { splitCitations, toBlocks } from "./citations";

interface Pinned {
  id: string;
  filename: string;
}

interface Turn {
  id: number;
  question: string;
  pinned: Pinned | null;
  phase: "queued" | "working" | "done" | "error";
  draft: string;
  answer?: Answer;
  error?: string;
  feedback?: 1 | -1;
}

export default function Chat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [pinned, setPinned] = useState<Pinned | null>(null);
  const [text, setText] = useState("");
  const abort = useRef<AbortController | null>(null);
  const nextId = useRef(1);
  const end = useRef<HTMLDivElement | null>(null);
  const busy = turns.some((t) => t.phase === "queued" || t.phase === "working");

  useEffect(() => () => abort.current?.abort(), []);
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "nearest" });
  }, [turns]);

  function patch(id: number, change: Partial<Turn>) {
    setTurns((all) => all.map((t) => (t.id === id ? { ...t, ...change } : t)));
  }

  function ask(question: string, pin: Pinned | null) {
    const q = question.trim();
    if (!q || busy) return;
    const id = nextId.current++;
    setTurns((all) => [...all, { id, question: q, pinned: pin, phase: "working", draft: "" }]);
    const controller = new AbortController();
    abort.current = controller;
    void streamChat(
      q,
      pin?.id ?? null,
      {
        onStatus: (state) => patch(id, { phase: state === "queued" ? "queued" : "working" }),
        onToken: (t) =>
          setTurns((all) => all.map((x) => (x.id === id ? { ...x, draft: x.draft + t } : x))),
        onAnswer: (answer) => patch(id, { phase: "done", answer, draft: "" }),
        onError: (error) => patch(id, { phase: "error", error, draft: "" }),
      },
      controller.signal,
    );
  }

  function submit(e?: FormEvent) {
    e?.preventDefault();
    ask(text, pinned);
    setText("");
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function choose(turn: Turn, c: Candidate) {
    const pin = { id: c.id, filename: c.filename };
    setPinned(pin);
    ask(turn.question, pin);
  }

  return (
    <section className="chat" aria-label="Ask a question">
      {turns.length === 0 && (
        <div className="empty">
          <h1>Ask about a document</h1>
          <p className="muted">
            Name the document and ask your question, for example: <em>“Boost Connect SR-1098 SOW —
            what is the scope of work mainly about?”</em> You&apos;ll get a short answer with
            citations and the original file to download.
          </p>
        </div>
      )}

      <ol className="turns">
        {turns.map((t) => (
          <li key={t.id} className="turn">
            <p className="q">{t.question}</p>
            {t.pinned && <p className="muted small">Asked about {t.pinned.filename}</p>}
            {t.phase === "queued" && (
              <p className="status" role="status">
                Waiting for the model. Other questions are ahead of yours…
              </p>
            )}
            {t.phase === "working" && (
              <div role="status">
                <p className="status">Searching the documents…</p>
                {t.draft && (
                  <p className="draft" aria-hidden="true">
                    <span className="tag">Draft, not yet checked against sources</span>
                    {t.draft}
                  </p>
                )}
              </div>
            )}
            {t.phase === "error" && (
              <p className="error" role="alert">
                {t.error}
              </p>
            )}
            {t.phase === "done" && t.answer && (
              <AnswerView
                turn={t}
                answer={t.answer}
                busy={busy}
                onChoose={(c) => choose(t, c)}
                onPin={(p) => setPinned(p)}
                onFeedback={(v) => {
                  patch(t.id, { feedback: v ?? undefined });
                  void sendFeedback(t.answer?.qa_id ?? 0, v).catch(() =>
                    patch(t.id, { feedback: undefined }),
                  );
                }}
              />
            )}
          </li>
        ))}
      </ol>
      <div ref={end} />

      <form className="composer" onSubmit={submit}>
        {pinned && (
          <p className="chip">
            Asking about <strong>{pinned.filename}</strong>
            <button type="button" className="link" onClick={() => setPinned(null)} aria-label="Stop asking about this document">
              ✕
            </button>
          </p>
        )}
        <label className="sr" htmlFor="question">
          Your question
        </label>
        <textarea
          id="question"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          rows={2}
          placeholder="Name a document and ask your question…"
          maxLength={2000}
        />
        <button type="submit" disabled={busy || !text.trim()}>
          Ask
        </button>
      </form>
    </section>
  );
}

function AnswerText({ text }: { text: string }) {
  return (
    <>
      {toBlocks(text).map((b, i) =>
        b.kind === "p" ? (
          <p key={i}>
            <Inline text={b.text} />
          </p>
        ) : (
          <ul key={i}>
            {b.items.map((item, j) => (
              <li key={j}>
                <Inline text={item} />
              </li>
            ))}
          </ul>
        ),
      )}
    </>
  );
}

function Inline({ text }: { text: string }) {
  return (
    <>
      {splitCitations(text).map((p, i) =>
        p.kind === "text" ? (
          <span key={i}>{p.text}</span>
        ) : (
          <sup key={i} className="cite" title={`Source ${p.n}`}>
            [{p.n}]
          </sup>
        ),
      )}
    </>
  );
}

function AnswerView(props: {
  turn: Turn;
  answer: Answer;
  busy: boolean;
  onChoose: (c: Candidate) => void;
  onPin: (p: Pinned) => void;
  onFeedback: (v: 1 | -1 | null) => void;
}) {
  const { turn, answer, busy } = props;
  return (
    <div className="answer" aria-live="polite">
      {answer.status === "needs_choice" ? (
        <>
          <p>{answer.text}</p>
          <ul className="picker">
            {answer.candidates.map((c) => (
              <li key={c.id}>
                <button type="button" disabled={busy} onClick={() => props.onChoose(c)}>
                  {c.filename}
                </button>
                <span className="muted small"> {c.folder}</span>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <>
          <AnswerText text={answer.text} />
          {answer.ungrounded && (
            <p className="banner">
              This answer has no verified citation. Check the source documents before relying on it.
            </p>
          )}
          {answer.sources.length > 0 && (
            <div className="sources">
              <h2>Sources</h2>
              {answer.sources.map((s) => (
                <SourceCard key={s.id} source={s} />
              ))}
            </div>
          )}
          {answer.resolved && answer.status === "answered" && turn.pinned?.id !== answer.resolved.id && (
            <p className="small">
              <button
                type="button"
                className="link"
                onClick={() =>
                  answer.resolved &&
                  props.onPin({ id: answer.resolved.id, filename: answer.resolved.filename })
                }
              >
                Keep asking about this document
              </button>
            </p>
          )}
          <div className="feedback" role="group" aria-label="Was this helpful?">
            <button
              type="button"
              className={turn.feedback === 1 ? "thumb on" : "thumb"}
              aria-pressed={turn.feedback === 1}
              aria-label="Helpful"
              onClick={() => props.onFeedback(turn.feedback === 1 ? null : 1)}
            >
              👍
            </button>
            <button
              type="button"
              className={turn.feedback === -1 ? "thumb on" : "thumb"}
              aria-pressed={turn.feedback === -1}
              aria-label="Not helpful"
              onClick={() => props.onFeedback(turn.feedback === -1 ? null : -1)}
            >
              👎
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function SourceCard({ source }: { source: Source }) {
  const [open, setOpen] = useState(false);
  const [list, setList] = useState<VersionInfo[] | null>(null);
  const [problem, setProblem] = useState("");

  function toggle() {
    setOpen(!open);
    if (!open && list === null) {
      versions(source.id)
        .then(setList)
        .catch(() => setProblem("Could not load the versions."));
    }
  }

  return (
    <article className="source">
      <div className="head">
        <strong>{source.filename}</strong>
        <span className="badge">{source.folder || "(root)"}</span>
        <span className="muted small">{source.refs.map((n) => `[${n}]`).join(" ")}</span>
      </div>
      {source.sections.length > 0 && (
        <p className="muted small">Sections: {source.sections.join("; ")}</p>
      )}
      {source.snippet && <blockquote>{source.snippet}</blockquote>}
      <div className="actions">
        <a className="button" href={downloadUrl(source.id)}>
          Download
        </a>
        {source.n_versions > 1 && (
          <button type="button" className="link" onClick={toggle} aria-expanded={open}>
            {source.n_versions} versions
          </button>
        )}
      </div>
      {open && (
        <ul className="versions">
          {problem && <li className="error">{problem}</li>}
          {list === null && !problem && <li className="muted">Loading…</li>}
          {list?.map((v) => (
            <li key={v.id}>
              <a href={downloadUrl(v.id)}>{v.filename}</a>{" "}
              <span className="muted small">
                {new Date(v.modified).toLocaleDateString()} {v.latest ? "· latest" : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
