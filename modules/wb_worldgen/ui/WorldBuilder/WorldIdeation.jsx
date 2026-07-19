import { useState, useEffect, useRef } from 'react';
import { api } from 'api';

// C4: the ideation conversation — the front door of world building. The
// player and the AI converge on what the world IS before the build agent
// takes over: the AI maintains two shared drafts as the chat proceeds — the
// seed prompt (the wizard's prompt field doubles as its editor) and the
// world rules (the build's evaluation rubric) — and flips `ready` when it
// judges the idea settled, which highlights the Go button (the offer).
// Going is never gated on the offer: the player's go-ahead is the approval
// moment, and going with zero chat turns is the quick path (a brief with
// empty rules — exactly the old direct launch). Conversation state is
// client-held and mirrored to localStorage so an Android PWA kill loses
// nothing; the server route is stateless.
const IDEATION_KEY = 'wb_worldgen_ideation';

function readSavedIdeation() {
  try {
    return JSON.parse(localStorage.getItem(IDEATION_KEY) || 'null') || {};
  } catch {
    return {};
  }
}

// The conversation is handed off when the build launches — the brief is
// server truth from then on. Exported so the wizard clears it at that moment.
export function clearSavedIdeation() {
  try { localStorage.removeItem(IDEATION_KEY); } catch { /* storage unavailable */ }
}

export default function WorldIdeation({ promptText, onPromptChange, scenarioId, onGo, starting }) {
  const [messages, setMessages] = useState(() => readSavedIdeation().messages || []);
  const [rules, setRules] = useState(() => readSavedIdeation().rules || []);
  const [notes, setNotes] = useState(() => readSavedIdeation().notes || []);
  const [ready, setReady] = useState(() => !!readSavedIdeation().ready);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const listRef = useRef(null);

  useEffect(() => {
    try {
      if (messages.length || rules.length || notes.length) {
        localStorage.setItem(IDEATION_KEY, JSON.stringify({ messages, rules, notes, ready }));
      } else {
        localStorage.removeItem(IDEATION_KEY);
      }
    } catch { /* storage unavailable */ }
  }, [messages, rules, notes, ready]);

  // Keep the transcript pinned to the newest message (and the thinking bubble).
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, busy]);

  const send = async () => {
    const text = input.trim();
    if (!text || busy || starting) return;
    const next = [...messages, { role: 'player', text }];
    setMessages(next);
    setInput('');
    setBusy(true);
    setError('');
    try {
      const res = await api.ideationTurn({
        messages: next,
        prompt: (promptText || '').trim() || null,
        rules,
        notes,
        scenarioId: scenarioId || null,
      });
      setMessages([...next, { role: 'assistant', text: res.reply }]);
      onPromptChange(res.prompt || '');
      setRules(res.rules || []);
      setNotes(res.notes || []);
      setReady(!!res.ready);
    } catch (e) {
      // Unanswered message comes back out of the transcript and into the
      // input, so sending again is the retry.
      setMessages(messages);
      setInput(text);
      setError(e.message || 'The design partner did not answer.');
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    if (busy) return;
    setMessages([]);
    setRules([]);
    setNotes([]);
    setReady(false);
    setError('');
    clearSavedIdeation();
  };

  // Dropping a rule is a hand edit like any other: the next turn's drafts
  // round-trip it, and Go sends exactly what is on screen.
  const removeRule = (i) => setRules((rs) => rs.filter((_, j) => j !== i));
  const removeNote = (i) => setNotes((ns) => ns.filter((_, j) => j !== i));

  const canGo = !!(promptText || '').trim() && !busy && !starting;

  return (
    <div className="rounded-lg border border-emerald-900/60 bg-emerald-950/10 p-3 space-y-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium text-gray-200">Design it with the AI</p>
          <p className="text-xs text-gray-500">
            Talk the world into shape — the AI keeps the prompt above and the world
            rules below updated as you go.
          </p>
        </div>
        {messages.length > 0 && (
          <button
            type="button"
            onClick={reset}
            disabled={busy}
            className="shrink-0 text-xs text-gray-500 hover:text-gray-300 disabled:opacity-50 transition-colors"
          >
            Start over
          </button>
        )}
      </div>

      {(messages.length > 0 || busy) && (
        <div ref={listRef} className="max-h-72 overflow-y-auto space-y-2 pr-1">
          {messages.map((m, i) => (
            <div key={i} className={m.role === 'player' ? 'flex justify-end' : 'flex justify-start'}>
              <div
                className={`max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
                  m.role === 'player' ? 'bg-emerald-900/40 text-gray-200' : 'bg-gray-800/80 text-gray-300'
                }`}
              >
                {m.text}
              </div>
            </div>
          ))}
          {busy && (
            <div className="flex justify-start">
              <div className="rounded-lg px-3 py-2 text-sm bg-gray-800/80 text-gray-500">Thinking…</div>
            </div>
          )}
        </div>
      )}

      <div className="flex gap-2 items-end">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={2}
          disabled={busy || starting}
          placeholder={messages.length ? 'Reply…' : 'What kind of world are you dreaming of?'}
          className="flex-1 bg-gray-900/80 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-emerald-600 resize-none"
        />
        <button
          type="button"
          onClick={send}
          disabled={!input.trim() || busy || starting}
          className="shrink-0 px-3 py-2 rounded-lg text-sm bg-gray-700 hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed text-gray-200 transition-colors"
        >
          Send
        </button>
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}

      {rules.length > 0 && (
        <div className="rounded-lg border border-gray-700 bg-gray-900/50 p-3 space-y-1.5">
          <p className="text-xs font-medium text-gray-400">
            World rules <span className="text-gray-600">— the build is judged against these</span>
          </p>
          {rules.map((r, i) => (
            <div key={i} className="flex items-start gap-2">
              <span className="text-sm text-gray-300 flex-1">• {r}</span>
              <button
                type="button"
                onClick={() => removeRule(i)}
                disabled={busy || starting}
                title="Drop this rule"
                className="shrink-0 text-gray-600 hover:text-red-400 disabled:opacity-50 text-xs transition-colors"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      {notes.length > 0 && (
        <div className="rounded-lg border border-gray-700 bg-gray-900/50 p-3 space-y-1.5">
          <p className="text-xs font-medium text-gray-400">
            Design notes <span className="text-gray-600">— established facts the build must honor; notes about a specific place steer only that place</span>
          </p>
          {notes.map((n, i) => (
            <div key={i} className="flex items-start gap-2">
              <span className="text-sm text-gray-300 flex-1">
                {n.subject && (
                  <span className="text-emerald-400/80 text-xs mr-1.5 border border-emerald-900 rounded px-1 py-0.5">{n.subject}</span>
                )}
                {n.text}
              </span>
              <button
                type="button"
                onClick={() => removeNote(i)}
                disabled={busy || starting}
                title="Drop this note"
                className="shrink-0 text-gray-600 hover:text-red-400 disabled:opacity-50 text-xs transition-colors"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      {ready && (
        <p className="text-xs text-emerald-300">
          The AI thinks this is ready to build — your call.
        </p>
      )}

      <div>
        <button
          type="button"
          onClick={() => onGo(rules, notes)}
          disabled={!canGo}
          className={`w-full py-3 rounded-lg font-medium text-lg transition-colors flex items-center justify-center gap-2 ${
            ready
              ? 'bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 disabled:cursor-not-allowed text-white'
              : 'border border-emerald-800 text-emerald-300 hover:bg-emerald-950/40 disabled:opacity-50 disabled:cursor-not-allowed'
          }`}
        >
          {starting && (
            <span className="inline-block w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
          )}
          {starting ? 'Starting the build…' : 'Build this world'}
        </button>
        <p className="text-xs text-gray-500 mt-2">
          An agent plans, builds and verifies the whole world on its own — watch it
          work, or come back later. It runs server-side, so closing the app doesn't
          stop it.
        </p>
      </div>
    </div>
  );
}
