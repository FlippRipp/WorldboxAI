import { useState, useEffect, useRef } from 'react';
import { api } from 'api';

// The agent session surface — since C7b, the one continuous screen world
// creation lives on. A session opens in the CHAT PHASE: the conversation
// fills the log, the shared drafts (prompt, rules, notes) render as
// editable panels beside it (hand edits PUT to the server — the drafts are
// server truth), and the Go button flips the same session into the BUILD
// PHASE: the C3 observer — live todo list, current action, streamed action
// log with expandable observations, evaluator findings, transient
// enrichment progress, cancel — with the input box persisting throughout
// (C7a: mid-build messages land at the next turn boundary; queued until
// then — mid-enrichment a reply can be minutes away). The SSE stream
// replays the persisted log from the last seen index and reconnects after
// drops; a dead chat session revives server-side on the next message, so
// even a backend restart only pauses the conversation.

const STATUS_LABEL = {
  running: 'building…',
  designing: 'designing…',   // the running chat phase (C7b)
  done: 'finished',
  cancelled: 'cancelled',
  failed: 'failed',
  budget_exhausted: 'stopped — budget exhausted',
};

const STATUS_STYLE = {
  running: 'bg-emerald-900/40 text-emerald-300 border-emerald-800',
  designing: 'bg-sky-900/40 text-sky-300 border-sky-800',
  done: 'bg-emerald-900/40 text-emerald-300 border-emerald-800',
  cancelled: 'bg-gray-800 text-gray-400 border-gray-700',
  failed: 'bg-red-950/40 text-red-300 border-red-900',
  budget_exhausted: 'bg-amber-950/40 text-amber-300 border-amber-900',
};

const TODO_ICON = { done: '✓', in_progress: '➤', pending: '○' };

function compactJson(value) {
  const text = JSON.stringify(value);
  return text && text.length > 120 ? null : text;
}

// One line of signal per tool result; the raw JSON stays behind a toggle.
function summarizeResult(result) {
  if (!result || typeof result !== 'object') return '';
  if (result.summary && typeof result.summary === 'object') {
    const s = result.summary;
    const bits = [];
    if (s.labeled) bits.push(`${s.labeled} labeled`);
    if (s.described) bits.push(`${s.described} described`);
    Object.keys(s).forEach((k) => {
      if (k.startsWith('custom_') && s[k]) bits.push(`${s[k]} × ${k}`);
    });
    if (s.review && typeof s.review === 'object') {
      bits.push(`review: ${s.review.reviewed_maps ?? 0} map(s), ${s.review.flagged ?? 0} flagged`);
    }
    if (s.failed_node_ids?.length) bits.push(`${s.failed_node_ids.length} failed`);
    if (s.cancelled) bits.push('cancelled');
    return bits.join(' · ') || 'run finished';
  }
  if (typeof result.summary === 'string') return result.summary;
  if (Array.isArray(result.findings)) {
    return result.clean ? 'evaluation clean' : `${result.blocking ?? 0} blocking finding(s)`;
  }
  if (Array.isArray(result.rules) && Array.isArray(result.added)) {
    const bits = [`${result.rules.length} rule(s)`];
    if (result.added.length) bits.push(`+${result.added.length}`);
    if (result.removed?.length) bits.push(`−${result.removed.length}`);
    return `rules updated: ${bits.join(' · ')}`;
  }
  if (Array.isArray(result.notes) && Array.isArray(result.edited)) {
    const bits = [];
    if (result.added?.length) bits.push(`added ${result.added.join(', ')}`);
    if (result.edited.length) bits.push(`edited ${result.edited.join(', ')}`);
    if (result.removed?.length) bits.push(`removed ${result.removed.map((r) => r.id).join(', ')}`);
    return `notes updated: ${bits.join(' · ') || 'no changes'}`;
  }
  if (typeof result.prompt === 'string' && typeof result.previous === 'string') return 'prompt updated';
  if (Array.isArray(result.exchanges)) return `${result.exchanges.length} exchange(s) read`;
  if (Array.isArray(result.maps)) {
    return result.maps.map((m) => `${m.label || m.map_id}: ${m.nodes} nodes, ${m.named} named`).join(' · ');
  }
  if (result.saved && result.step_id) return `saved ${result.step_id}`;
  if (result.updated) return `updated ${Object.keys(result.updated).join(', ')}`;
  if (Array.isArray(result.node_list)) return `${result.nodes} nodes, ${result.named} named`;
  if (result.node?.id) return `${result.node.name || result.node.id}`;
  if (Array.isArray(result.problems)) {
    return result.clean ? 'lint clean' : `${result.problem_count} lint problem(s)`;
  }
  if (result.markdown) return 'catalog';
  return '';
}

function findingsOf(observation) {
  if (!observation) return null;
  if (Array.isArray(observation.blocking_findings) && observation.blocking_findings.length) {
    return observation.blocking_findings;
  }
  const findings = observation.result?.findings;
  return Array.isArray(findings) && findings.length ? findings : null;
}

function progressLine(evt) {
  if (!evt) return null;
  if (evt.type === 'node' || evt.type === 'failed') {
    return `${evt.phase}: ${evt.total_labeled ?? '?'}/${evt.total_nodes ?? '?'}`;
  }
  if (evt.type === 'phase') return `${evt.phase}: ${evt.pending} pending`;
  if (evt.type === 'review_fix') return `review fix: ${evt.old} → ${evt.new}`;
  if (evt.type === 'pregenerated') return `pregenerated ${evt.name}`;
  if (evt.type === 'verifier_action') {
    return `note verifier: ${evt.tool}${evt.discussing ? ` (discussing ${evt.discussing})` : ''}`;
  }
  return null;
}

// The end-of-build review (C5/N7): compromises and accepted note
// obligations, each vetoable. Doing nothing keeps the world as built; a
// veto relaunches the agent with the original notes binding.
function NotesReviewPanel({ review, onVeto, vetoing }) {
  const [checked, setChecked] = useState({});
  const items = [
    ...(review.amended || []).map((a) => ({ ...a, kind: 'amended' })),
    ...(review.accepted_notes || []).map((a) => ({ ...a, kind: 'accepted' })),
  ];
  if (!items.length) return null;
  const ids = items.filter((it) => checked[it.id]).map((it) => it.id);
  return (
    <div className="text-sm bg-amber-950/20 border border-amber-900/60 rounded-lg p-3 space-y-2">
      <p className="text-amber-200 font-medium">
        Review: the build changed or gave up on {items.length} of your notes
      </p>
      <p className="text-xs text-gray-500">
        Keeping the world as built needs no action. Tick what you reject and
        enforce it — the agent rebuilds until the original note is honored,
        and a vetoed note can never be compromised again.
      </p>
      <ul className="space-y-2">
        {items.map((it) => (
          <li key={it.id} className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={!!checked[it.id]}
              onChange={(e) => setChecked((c) => ({ ...c, [it.id]: e.target.checked }))}
              disabled={vetoing}
              className="mt-1 accent-amber-500"
            />
            <div className="text-xs flex-1 space-y-0.5">
              {it.subject && (
                <span className="text-emerald-400/80 border border-emerald-900 rounded px-1 py-0.5 mr-1.5">{it.subject}</span>
              )}
              {it.kind === 'amended' ? (
                <>
                  <span className="text-gray-500 line-through">{it.original_text}</span>
                  <span className="text-gray-300"> → {it.amended_text}</span>
                  {it.rationale && <p className="text-gray-500 italic">verifier: {it.rationale}</p>}
                </>
              ) : (
                <>
                  <span className="text-gray-300">{it.text}</span>
                  <p className="text-amber-400/80">not honored — accepted by the agent{it.reason ? `: ${it.reason}` : ''}</p>
                </>
              )}
            </div>
          </li>
        ))}
      </ul>
      <button
        onClick={() => onVeto(ids)}
        disabled={!ids.length || vetoing}
        className="px-3 py-1.5 rounded-lg border border-amber-800 text-amber-300 hover:bg-amber-950/40 disabled:opacity-50 disabled:cursor-not-allowed text-sm transition-colors"
      >
        {vetoing ? 'Relaunching…' : `Veto ${ids.length || ''} and rebuild`}
      </button>
    </div>
  );
}

// A chat bubble in the action log (C7a): the user's message (right, sky)
// or the agent's `say` reply (left, emerald). `queued`/`unread` annotate
// the two off-nominal states of a user message.
function ChatBubble({ who, text, queued, unread }) {
  const user = who === 'user';
  return (
    <div className={`flex pt-1 ${user ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-1.5 border ${
          user
            ? `rounded-br-sm ${queued ? 'bg-sky-950/30 border-sky-900/50 border-dashed' : 'bg-sky-900/40 border-sky-800/70'}`
            : 'rounded-bl-sm bg-emerald-950/40 border-emerald-900/70'
        }`}
      >
        <p className={`text-sm whitespace-pre-wrap break-words ${
          user ? (queued ? 'text-sky-200/70' : 'text-sky-100') : 'text-emerald-100'
        }`}>{text}</p>
        {queued && (
          <p className="text-[11px] text-gray-500 mt-0.5">queued — the agent reads it after the current action</p>
        )}
        {unread && (
          <p className="text-[11px] text-amber-400/80 mt-0.5">the build ended before the agent read this</p>
        )}
      </div>
    </div>
  );
}

// The agent's typing indicator: an agent-side bubble with three pulsing
// dots, shown in the chat phase while a reply completion is in flight.
function TypingBubble() {
  return (
    <div className="flex pt-1 justify-start">
      <div className="rounded-lg rounded-bl-sm px-3 py-2.5 border bg-emerald-950/40 border-emerald-900/70 flex items-center gap-1">
        {[0, 200, 400].map((delay) => (
          <span
            key={delay}
            className="w-1.5 h-1.5 rounded-full bg-emerald-300/80 animate-bounce"
            style={{ animationDelay: `${delay}ms`, animationDuration: '1s' }}
          />
        ))}
      </div>
    </div>
  );
}

function LogRow({ evt }) {
  const [open, setOpen] = useState(false);
  if (evt.type === 'user_message') {
    return <ChatBubble who="user" text={evt.text} unread={evt.unread} />;
  }
  if (evt.type === 'phase') {
    // The Go moment (C7b): the session flipped from chat to build.
    return (
      <div className="flex items-center gap-2 py-2 text-[11px] uppercase tracking-wider text-emerald-500/80">
        <span className="flex-1 border-t border-emerald-900/60" />
        the build started
        <span className="flex-1 border-t border-emerald-900/60" />
      </div>
    );
  }
  if (evt.type === 'turn') {
    if (evt.phase === 'chat') {
      // A design-conversation reply: just the bubble — no turn chrome.
      return evt.say ? <ChatBubble who="agent" text={evt.say} /> : null;
    }
    return (
      <div className="pt-3 first:pt-0">
        <div className="text-[11px] uppercase tracking-wider text-gray-600">Turn {evt.turn}</div>
        {evt.thought && <div className="text-xs text-gray-500 italic">{evt.thought}</div>}
        {evt.say && <ChatBubble who="agent" text={evt.say} />}
      </div>
    );
  }
  if (evt.type === 'action') {
    const args = evt.args && Object.keys(evt.args).length ? compactJson(evt.args) : null;
    return (
      <div className="text-sm text-gray-300 pl-3 break-words">
        ▸ <span className="text-purple-300 font-medium">{evt.tool}</span>
        {args && <span className="text-gray-500 text-xs"> {args}</span>}
        {evt.args && Object.keys(evt.args).length > 0 && !args && (
          <button onClick={() => setOpen(!open)} className="text-xs text-gray-500 hover:text-gray-300 ml-2">
            {open ? 'hide args' : 'args…'}
          </button>
        )}
        {open && <pre className="text-[11px] text-gray-500 bg-gray-900/60 rounded p-2 mt-1 whitespace-pre-wrap break-words">{JSON.stringify(evt.args, null, 2)}</pre>}
      </div>
    );
  }
  if (evt.type === 'observation') {
    const findings = findingsOf(evt);
    const summary = evt.ok ? summarizeResult(evt.result) : null;
    return (
      <div className="pl-6 text-xs space-y-1">
        {evt.ok ? (
          <div className="text-gray-400">
            <span className="text-emerald-500">→</span> {summary || 'ok'}
            {evt.result && (
              <button onClick={() => setOpen(!open)} className="text-gray-600 hover:text-gray-300 ml-2">
                {open ? 'hide' : 'detail…'}
              </button>
            )}
          </div>
        ) : (
          <div className="text-red-400">
            → {evt.error || evt.protocol_error || evt.message || 'rejected'}
          </div>
        )}
        {findings && (
          <ul className="space-y-1 border-l-2 border-amber-900/60 pl-2">
            {findings.map((f, i) => (
              <li key={i} className="text-amber-200/90">
                <span className="text-amber-500">{f.severity === 'nit' ? '◦' : '⚑'}</span>{' '}
                <span className="text-amber-400/80">[{f.kind}]</span> {f.finding}
                {f.suggestion && <span className="text-gray-500"> — {f.suggestion}</span>}
              </li>
            ))}
          </ul>
        )}
        {open && evt.result && (
          <pre className="text-[11px] text-gray-500 bg-gray-900/60 rounded p-2 whitespace-pre-wrap break-words">{JSON.stringify(evt.result, null, 2)}</pre>
        )}
      </div>
    );
  }
  if (evt.type === 'eval') {
    return (
      <div className={`pl-6 text-xs ${evt.clean ? 'text-emerald-400' : 'text-amber-300'}`}>
        ⚖ evaluation ({evt.trigger === 'done_claim' ? 'done-gate' : 'requested'}):{' '}
        {evt.clean ? 'clean' : `${evt.blocking} blocking of ${evt.findings} finding(s)`}
      </div>
    );
  }
  return null;
}

// The shared drafts as editable panels (C7b, chat phase only): the drafts
// are server truth, so the prompt field PUTs on blur and each rule/note ✕
// PUTs the filtered list. After Go the panels go read-only — mid-build the
// user's channel is the conversation (the agent carries their words into
// the brief through the brief tools).
function ChatDraftPanels({ worldId, brief, disabled, onSaved }) {
  const [promptDraft, setPromptDraft] = useState(brief?.prompt || '');
  const dirtyRef = useRef(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  // Server updates (the agent's update_prompt, a reattach) flow into the
  // field unless the user is mid-edit — their keystrokes win.
  useEffect(() => {
    if (!dirtyRef.current) setPromptDraft(brief?.prompt || '');
  }, [brief?.prompt]);

  const put = async (fields) => {
    setSaving(true);
    setError('');
    try {
      const res = await api.agentBrief(worldId, fields);
      onSaved?.(res.brief);
      return true;
    } catch (e) {
      setError(e.message || 'Saving the draft failed.');
      return false;
    } finally {
      setSaving(false);
    }
  };

  const savePrompt = async () => {
    if (!dirtyRef.current) return;
    if ((promptDraft || '') === (brief?.prompt || '')) { dirtyRef.current = false; return; }
    if (await put({ prompt: promptDraft })) dirtyRef.current = false;
  };
  const removeRule = (i) => put({ rules: (brief?.rules || []).filter((_, j) => j !== i) });
  const removeNote = (i) => put({ notes: (brief?.notes || []).filter((_, j) => j !== i) });

  const rules = brief?.rules || [];
  const notes = brief?.notes || [];
  return (
    <div className="space-y-3">
      <div>
        <label className="block text-xs font-medium text-gray-400 mb-1">
          World prompt <span className="text-gray-600">— the seed the generator expands; the AI keeps it updated as you talk</span>
        </label>
        <textarea
          value={promptDraft}
          onChange={(e) => { dirtyRef.current = true; setPromptDraft(e.target.value); }}
          onBlur={savePrompt}
          rows={3}
          disabled={disabled || saving}
          placeholder="No seed prompt yet — describe the world in the chat, or type one here."
          className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:border-emerald-600 focus:outline-none resize-y"
        />
      </div>
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
                disabled={disabled || saving}
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
            <div key={n.id || i} className="flex items-start gap-2">
              <span className="text-sm text-gray-300 flex-1">
                {n.subject && (
                  <span className="text-emerald-400/80 text-xs mr-1.5 border border-emerald-900 rounded px-1 py-0.5">{n.subject}</span>
                )}
                {n.text}
              </span>
              <button
                type="button"
                onClick={() => removeNote(i)}
                disabled={disabled || saving}
                title="Drop this note"
                className="shrink-0 text-gray-600 hover:text-red-400 disabled:opacity-50 text-xs transition-colors"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  );
}

export default function AgentBuildObserver({ worldId, onDismiss, onOpenWorlds, onExplore, onBack }) {
  const [meta, setMeta] = useState(null);        // status snapshot (seed prompt etc.)
  const [events, setEvents] = useState([]);      // persisted, i-ordered
  const [progress, setProgress] = useState(null);
  const [terminal, setTerminal] = useState(null);
  const [gone, setGone] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [vetoing, setVetoing] = useState(false);
  // Messages sent but not yet drained by the agent (C7a). Their bubbles
  // render as "queued" until the matching user_message event lands; the
  // status snapshot re-seeds them on reattach.
  const [pending, setPending] = useState([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [going, setGoing] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  // Bumped when a veto relaunches the build (or a message revives a dead
  // chat session): both effects re-run and the observer follows the new
  // stream from where it left off.
  const [streamEpoch, setStreamEpoch] = useState(0);
  const lastIRef = useRef(-1);
  const logRef = useRef(null);
  const briefEditsRef = useRef(0);

  // One snapshot up front: the prompt/counters paint before the replay
  // lands, and a 404 marks the stale reference for dismissal.
  useEffect(() => {
    let alive = true;
    api.agentBuildStatus(worldId)
      .then((st) => {
        if (!alive) return;
        setMeta(st);
        // Reattach: messages queued server-side before this mount keep
        // their bubbles (merge — a message sent during the fetch stays).
        const queued = st.queued_messages || [];
        setPending((p) => [...queued.filter((m) => !p.some((x) => x.id === m.id)), ...p]);
      })
      .catch((e) => { if (alive && e.status === 404) setGone(true); });
    return () => { alive = false; };
  }, [worldId, streamEpoch]);

  // The event stream: full replay on mount, cursor replay + live after
  // drops. The loop runs server-side — closing this view changes nothing.
  useEffect(() => {
    let alive = true;
    let timer = null;
    let ctrl = null;
    const connect = async () => {
      ctrl = new AbortController();
      try {
        const final = await api.agentBuildEvents(
          worldId, { after: lastIRef.current + 1 },
          (evt) => {
            if (!alive) return;
            if (evt.type === 'progress') { setProgress(evt.event || null); return; }
            if (typeof evt.i === 'number') {
              lastIRef.current = Math.max(lastIRef.current, evt.i);
              // Events arrive index-ordered; drop anything already appended
              // (overlapping replays, dev double-mounted effects).
              setEvents((prev) => (
                prev.length && prev[prev.length - 1].i >= evt.i ? prev : [...prev, evt]
              ));
              // A resumed chat session (C7b) continues past an old terminal
              // event — anything newer than it means the session lives.
              if (evt.type !== 'done') {
                setTerminal((t) => (
                  t && typeof t.i === 'number' && evt.i > t.i ? null : t
                ));
              }
            }
            if (evt.type === 'observation') setProgress(null);
            if (evt.type === 'done') setTerminal(evt);
          }, ctrl.signal);
        if (!alive || final) return;
        timer = setTimeout(connect, 2000);   // stream dropped mid-build
      } catch (e) {
        if (!alive) return;
        if (e.status === 404) { setGone(true); return; }
        if (e.name === 'AbortError') return;
        timer = setTimeout(connect, 3000);
      }
    };
    connect();
    return () => { alive = false; clearTimeout(timer); ctrl?.abort(); };
  }, [worldId, streamEpoch]);

  // The displayed brief comes from the status snapshot; a successful
  // brief-edit tool call (U2 — the agent carrying the user's words into
  // the contract) or the Go flip (note ids are assigned there, N1)
  // refetches it so the contract on screen stays current.
  useEffect(() => {
    const edits = events.filter((e) => (
      (e.type === 'action'
        && ['update_prompt', 'update_rules', 'update_notes'].includes(e.tool)
        && events.some((o) => o.type === 'observation' && o.ok && o.i === e.i + 1))
      || e.type === 'phase'
    )).length;
    if (edits > briefEditsRef.current) {
      briefEditsRef.current = edits;
      api.agentBuildStatus(worldId).then(setMeta).catch(() => {});
    }
  }, [events, worldId]);

  // Queued bubbles: everything sent whose user_message event hasn't landed
  // yet. Derived at render time, so replay/live races can't double-show a
  // message — once the event is in the log, the log's bubble is the truth.
  const landedIds = new Set(
    events.filter((e) => e.type === 'user_message').map((e) => e.id),
  );
  const queuedMessages = pending.filter((m) => !landedIds.has(m.id));

  const status = terminal ? terminal.status : (gone ? 'gone' : 'running');
  const running = status === 'running' && !gone;
  // The session's phase (C7b): the last phase event wins (the Go flip is
  // an event), else the snapshot's word; old build artifacts have neither.
  const lastPhaseEvt = [...events].reverse().find((e) => e.type === 'phase');
  const phase = lastPhaseEvt?.phase || terminal?.phase || meta?.phase || 'build';
  const inChat = phase === 'chat' && !gone;
  // The chat agent's standing go offer — highlights Go, never gates it.
  const lastOfferEvt = [...events].reverse().find(
    (e) => e.type === 'turn' && e.phase === 'chat' && typeof e.ready === 'boolean',
  );
  const ready = lastOfferEvt ? lastOfferEvt.ready : !!meta?.ready;
  // Typing indicator (chat phase): a reply completion is in flight whenever
  // the newest chat event still expects one — a drained user_message, an
  // action whose result feeds the next completion, or an observation the
  // mini-loop continues past (ok, or a protocol error it retries). A turn
  // with no follow-up or a terminal error observation means the agent is
  // done talking. A message still queued client-side counts too — the
  // server drains it the moment the session picks it up.
  const lastChatEvt = [...events].reverse().find((e) => e.phase === 'chat');
  const agentTyping = inChat && running && !!(
    queuedMessages.length
    || (lastChatEvt && (
      lastChatEvt.type === 'user_message'
      || lastChatEvt.type === 'action'
      || (lastChatEvt.type === 'observation'
        && (lastChatEvt.ok || lastChatEvt.protocol_error))
    ))
  );

  // Keep the log pinned to the newest entry (the typing bubble counts —
  // it appears at the bottom like any new row).
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length, queuedMessages.length, agentTyping]);
  const turnEvents = events.filter((e) => e.type === 'turn' && e.phase !== 'chat');
  const chatTurns = Math.max(
    events.filter((e) => e.type === 'turn' && e.phase === 'chat').length,
    meta?.chat_turns ?? 0,
  );
  const todo = turnEvents.length ? turnEvents[turnEvents.length - 1].todo : (meta?.todo || []);
  const turns = terminal?.turns ?? (turnEvents.length ? turnEvents[turnEvents.length - 1].turn : meta?.turns ?? 0);
  const toolCalls = terminal?.tool_calls ?? events.filter((e) => e.type === 'action' && e.phase !== 'chat').length;
  const lastAction = [...events].reverse().find((e) => e.type === 'action');
  const actionPending = running && lastAction
    && !events.some((e) => e.type === 'observation' && e.i > lastAction.i);
  const result = terminal?.result || meta?.result;
  const brief = meta?.brief;

  const cancel = async () => {
    setCancelling(true);
    try { await api.agentBuildCancel(worldId); } catch { /* build may already be over */ }
  };

  // Speak into the session: in the chat phase this IS the conversation; in
  // the build the text queues server-side and reaches the agent at the
  // next turn boundary (C7a) — mid-action, a reply can be minutes away,
  // which is what the queued bubble state says. A dead chat session is
  // revived server-side by the message (C7b) — reattach the stream then.
  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      const res = await api.agentMessage(worldId, text);
      setPending((p) => [...p, { id: res.id, text }]);
      setDraft('');
      if (terminal) {
        setTerminal(null);
        setStreamEpoch((n) => n + 1);
      }
    } catch (e) {
      alert('Message failed: ' + e.message);
    } finally {
      setSending(false);
    }
  };

  // Go (C7b): flip this session into the self-driving build. The phase
  // event does the UI work; the button just asks.
  const go = async () => {
    if (going) return;
    setGoing(true);
    try {
      await api.agentGo(worldId);
    } catch (e) {
      alert('Failed to start the build: ' + e.message);
    } finally {
      setGoing(false);
    }
  };

  // Discarding an ideation draft deletes the world it lazily created —
  // the explicit-discard cleanup story (C7 fork 2); nothing sweeps drafts
  // behind the user's back.
  const discard = async () => {
    if (discarding) return;
    if (!window.confirm('Discard this draft world and its conversation? This cannot be undone.')) return;
    setDiscarding(true);
    try {
      await api.deleteWorld(worldId);
      onDismiss?.();
    } catch (e) {
      alert('Failed to discard the draft: ' + e.message);
      setDiscarding(false);
    }
  };

  // The veto (N7): relaunch with the rejected notes binding, then follow
  // the fix run from scratch — same world id, a fresh build and stream.
  const veto = async (noteIds) => {
    setVetoing(true);
    try {
      await api.agentVeto(worldId, noteIds);
      lastIRef.current = -1;
      setEvents([]);
      setTerminal(null);
      setProgress(null);
      setMeta(null);
      setCancelling(false);
      setPending([]);
      setStreamEpoch((n) => n + 1);
    } catch (e) {
      alert('Veto failed: ' + e.message);
    } finally {
      setVetoing(false);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center p-6">
      <div className="w-full max-w-3xl mt-10 space-y-4">
        {onBack && (
          <button
            onClick={onBack}
            className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            {inChat ? 'Back — the conversation stays saved'
              : running ? 'Back — the build keeps running' : 'Back'}
          </button>
        )}
        <div className="bg-gray-800/60 border border-gray-700 rounded-xl p-6 space-y-4">

          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <h2 className="text-2xl font-bold text-gray-100 flex items-center gap-3">
                {running && !inChat && (
                  <span className="inline-block w-5 h-5 border-2 border-emerald-400/30 border-t-emerald-400 rounded-full animate-spin" />
                )}
                {inChat ? 'Design the world' : 'Agent build'}
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full border ${STATUS_STYLE[inChat && running ? 'designing' : status] || 'bg-gray-800 text-gray-400 border-gray-700'}`}>
                  {gone ? 'not found'
                    : (STATUS_LABEL[inChat && running ? 'designing' : status] || status)}
                </span>
              </h2>
              <p className="text-gray-500 text-xs mt-1">
                World <span className="text-gray-400">{worldId}</span>
                {inChat
                  ? <>{' · '}{chatTurns} repl{chatTurns === 1 ? 'y' : 'ies'}</>
                  : <>{' · '}turn {turns}{' · '}{toolCalls} tool calls</>}
              </p>
            </div>
            <div className="flex gap-2 shrink-0">
              {inChat && (
                <button
                  onClick={discard}
                  disabled={discarding}
                  className="px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:bg-red-950/40 hover:text-red-300 hover:border-red-900 disabled:opacity-50 text-sm transition-colors"
                >
                  {discarding ? 'Discarding…' : 'Discard draft'}
                </button>
              )}
              {running && !gone && !inChat && (
                <button
                  onClick={cancel}
                  disabled={cancelling}
                  className="px-3 py-1.5 rounded-lg border border-red-900 text-red-300 hover:bg-red-950/40 disabled:opacity-50 text-sm transition-colors"
                >
                  {cancelling ? 'Cancelling…' : 'Cancel build'}
                </button>
              )}
              {!running && status === 'done' && onExplore && (
                <button
                  onClick={onExplore}
                  className="px-3 py-1.5 rounded-lg bg-emerald-700 hover:bg-emerald-600 text-sm font-medium transition-colors"
                >
                  Explore world
                </button>
              )}
              {!running && status === 'done' && (
                <button
                  onClick={onOpenWorlds}
                  className="px-3 py-1.5 rounded-lg border border-emerald-800 text-emerald-300 hover:bg-emerald-950/40 text-sm transition-colors"
                >
                  World list
                </button>
              )}
              {!running && (
                <button
                  onClick={onDismiss}
                  className="px-3 py-1.5 rounded-lg border border-gray-700 text-gray-400 hover:bg-gray-700 text-sm transition-colors"
                >
                  Dismiss
                </button>
              )}
            </div>
          </div>

          {!inChat && meta?.seed_prompt && (
            <p className="text-sm text-gray-400 border-l-2 border-gray-700 pl-3">{meta.seed_prompt}</p>
          )}

          {!inChat && brief?.rules?.length > 0 && (
            <div className="text-xs border-l-2 border-emerald-900 pl-3 space-y-0.5">
              <p className="text-gray-400 font-medium">
                Co-authored rules <span className="text-gray-600">— the build is judged against these</span>
              </p>
              {brief.rules.map((r, i) => (
                <p key={i} className="text-gray-500">• {r}</p>
              ))}
            </div>
          )}

          {!inChat && brief?.notes?.length > 0 && (
            <div className="text-xs border-l-2 border-emerald-900 pl-3 space-y-0.5">
              <p className="text-gray-400 font-medium">
                Design notes <span className="text-gray-600">— verified before the build can finish</span>
              </p>
              {brief.notes.map((n, i) => (
                <p key={i} className="text-gray-500">
                  •{' '}
                  {n.subject && <span className="text-emerald-500/80">[{n.subject}]</span>}
                  {' '}{n.text}
                  {n.status === 'amended' && <span className="text-amber-500/90"> (amended — review pending)</span>}
                  {n.no_compromise && <span className="text-amber-500/90"> (vetoed — binding as written)</span>}
                </p>
              ))}
            </div>
          )}

          {gone && (
            <p className="text-sm text-gray-400">
              No agent build exists for this world — it may have been deleted.
            </p>
          )}

          {todo.length > 0 && (
            <ul className="space-y-1">
              {todo.map((item, i) => (
                <li
                  key={i}
                  className={`text-sm flex gap-2 ${
                    item.status === 'done' ? 'text-gray-500 line-through'
                      : item.status === 'in_progress' ? 'text-emerald-300'
                        : 'text-gray-300'
                  }`}
                >
                  <span className="w-4 text-center shrink-0">{TODO_ICON[item.status] || '○'}</span>
                  {item.text}
                </li>
              ))}
            </ul>
          )}

          {running && (actionPending || progress) && (
            <div className="text-xs text-emerald-300/90 bg-emerald-950/30 border border-emerald-900/50 rounded-lg px-3 py-2 flex items-center gap-2">
              <span className="inline-block w-3 h-3 border border-emerald-400/40 border-t-emerald-300 rounded-full animate-spin" />
              {actionPending ? `Running ${lastAction.tool}` : 'Working'}
              {progress && progressLine(progress) ? ` — ${progressLine(progress)}` : '…'}
            </div>
          )}

          {status === 'done' && result && (
            <div className="text-sm bg-emerald-950/30 border border-emerald-900/60 rounded-lg p-3 space-y-2">
              <p className="text-emerald-300">{result.summary}</p>
              {result.accepted_findings?.length > 0 && (
                <div className="text-xs text-gray-400">
                  <div className="text-gray-500 mb-1">Accepted findings:</div>
                  <ul className="space-y-1">
                    {result.accepted_findings.map((f, i) => (
                      <li key={i}>
                        ⚑ {f.finding}
                        {f.auto
                          ? <span className="text-amber-500"> (auto-accepted after fix budget)</span>
                          : f.note && <span className="text-gray-500"> — {f.note}</span>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {result.open_nits?.length > 0 && (
                <div className="text-xs text-gray-500">
                  {result.open_nits.length} minor note(s) left open — see the log's last evaluation.
                </div>
              )}
            </div>
          )}
          {status === 'done' && result?.pending_review && (
            <NotesReviewPanel review={result.pending_review} onVeto={veto} vetoing={vetoing} />
          )}
          {status === 'failed' && (
            <p className="text-sm text-red-400">{terminal?.error || meta?.error || 'The build failed.'}</p>
          )}
          {status === 'budget_exhausted' && (
            <p className="text-sm text-amber-300/90">
              The build hit its budget before finishing. The world remains as an
              in-progress draft — finish it in the wizard, or start a new build.
            </p>
          )}

          <div>
            <div className="text-[11px] uppercase tracking-wider text-gray-600 mb-1">
              {inChat ? 'Conversation' : 'Action log'}
            </div>
            <div ref={logRef} className="bg-gray-900/50 border border-gray-800 rounded-lg p-3 max-h-96 overflow-y-auto book-scroll space-y-1.5">
              {events.length === 0 && queuedMessages.length === 0 && (
                <p className="text-xs text-gray-600">
                  {gone ? 'No log.' : inChat ? 'Waiting for the reply…' : 'Waiting for the first turn…'}
                </p>
              )}
              {events.map((evt) => <LogRow key={evt.i} evt={evt} />)}
              {queuedMessages.map((m) => (
                <ChatBubble key={m.id} who="user" text={m.text} queued />
              ))}
              {agentTyping && <TypingBubble />}
            </div>
          </div>

          {(running || inChat) && !gone && (
            <form
              onSubmit={(e) => { e.preventDefault(); send(); }}
              className="flex gap-2"
            >
              <input
                type="text"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={inChat
                  ? 'Talk the world into shape — the AI keeps the drafts below updated…'
                  : 'Say something to the agent — it reads at the next turn…'}
                disabled={sending}
                className="flex-1 bg-gray-900/70 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-sky-700 transition-colors"
              />
              <button
                type="submit"
                disabled={sending || !draft.trim()}
                className="px-3 py-2 rounded-lg border border-sky-800 text-sky-300 hover:bg-sky-950/40 disabled:opacity-50 disabled:cursor-not-allowed text-sm transition-colors"
              >
                {sending ? 'Sending…' : 'Send'}
              </button>
            </form>
          )}

          {inChat && (
            <>
              <ChatDraftPanels
                worldId={worldId}
                brief={brief}
                disabled={discarding || going}
                onSaved={(newBrief) => setMeta((m) => ({ ...(m || {}), brief: newBrief, seed_prompt: newBrief?.prompt ?? m?.seed_prompt }))}
              />
              {ready && (
                <p className="text-xs text-emerald-300">
                  The AI thinks this is ready to build — your call.
                </p>
              )}
              <div>
                <button
                  type="button"
                  onClick={go}
                  disabled={going || discarding || !(brief?.prompt || '').trim()}
                  className={`w-full py-3 rounded-lg font-medium text-lg transition-colors flex items-center justify-center gap-2 ${
                    ready
                      ? 'bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 disabled:cursor-not-allowed text-white'
                      : 'border border-emerald-800 text-emerald-300 hover:bg-emerald-950/40 disabled:opacity-50 disabled:cursor-not-allowed'
                  }`}
                >
                  {going && (
                    <span className="inline-block w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  )}
                  {going ? 'Starting the build…' : 'Build this world'}
                </button>
                <p className="text-xs text-gray-500 mt-2">
                  An agent plans, builds and verifies the whole world on its own — keep
                  talking to it while it works. It runs server-side, so closing the app
                  doesn't stop it.
                </p>
              </div>
            </>
          )}

        </div>
      </div>
    </div>
  );
}
