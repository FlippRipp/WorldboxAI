import { useState, useEffect, useRef } from 'react';
import { api } from 'api';

// The build observer (C3): the watching surface for a server-side agent
// build. Live todo list, current action, the streamed action log with
// expandable observations, evaluator findings, transient enrichment
// progress, cancel, and reattach — the SSE stream replays the persisted
// log from the last seen index and reconnects after drops, so a relaunched
// client recovers the full picture from the running backend.

const STATUS_LABEL = {
  running: 'building…',
  done: 'finished',
  cancelled: 'cancelled',
  failed: 'failed',
  budget_exhausted: 'stopped — budget exhausted',
};

const STATUS_STYLE = {
  running: 'bg-emerald-900/40 text-emerald-300 border-emerald-800',
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
  return null;
}

function LogRow({ evt }) {
  const [open, setOpen] = useState(false);
  if (evt.type === 'turn') {
    return (
      <div className="pt-3 first:pt-0">
        <div className="text-[11px] uppercase tracking-wider text-gray-600">Turn {evt.turn}</div>
        {evt.thought && <div className="text-xs text-gray-500 italic">{evt.thought}</div>}
      </div>
    );
  }
  if (evt.type === 'action') {
    const args = evt.args && Object.keys(evt.args).length ? compactJson(evt.args) : null;
    return (
      <div className="text-sm text-gray-300 pl-3">
        ▸ <span className="text-purple-300 font-medium">{evt.tool}</span>
        {args && <span className="text-gray-500 text-xs"> {args}</span>}
        {evt.args && Object.keys(evt.args).length > 0 && !args && (
          <button onClick={() => setOpen(!open)} className="text-xs text-gray-500 hover:text-gray-300 ml-2">
            {open ? 'hide args' : 'args…'}
          </button>
        )}
        {open && <pre className="text-[11px] text-gray-500 bg-gray-900/60 rounded p-2 mt-1 overflow-x-auto">{JSON.stringify(evt.args, null, 2)}</pre>}
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
          <pre className="text-[11px] text-gray-500 bg-gray-900/60 rounded p-2 overflow-x-auto max-h-64 overflow-y-auto">{JSON.stringify(evt.result, null, 2)}</pre>
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

export default function AgentBuildObserver({ worldId, onDismiss, onOpenWorlds, onBack }) {
  const [meta, setMeta] = useState(null);        // status snapshot (seed prompt etc.)
  const [events, setEvents] = useState([]);      // persisted, i-ordered
  const [progress, setProgress] = useState(null);
  const [terminal, setTerminal] = useState(null);
  const [gone, setGone] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const lastIRef = useRef(-1);
  const logRef = useRef(null);

  // One snapshot up front: the prompt/counters paint before the replay
  // lands, and a 404 marks the stale reference for dismissal.
  useEffect(() => {
    let alive = true;
    api.agentBuildStatus(worldId)
      .then((st) => { if (alive) setMeta(st); })
      .catch((e) => { if (alive && e.status === 404) setGone(true); });
    return () => { alive = false; };
  }, [worldId]);

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
  }, [worldId]);

  // Keep the log pinned to the newest entry.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  const status = terminal ? terminal.status : (gone ? 'gone' : 'running');
  const running = status === 'running' && !gone;
  const turnEvents = events.filter((e) => e.type === 'turn');
  const todo = turnEvents.length ? turnEvents[turnEvents.length - 1].todo : (meta?.todo || []);
  const turns = terminal?.turns ?? (turnEvents.length ? turnEvents[turnEvents.length - 1].turn : meta?.turns ?? 0);
  const toolCalls = terminal?.tool_calls ?? events.filter((e) => e.type === 'action').length;
  const lastAction = [...events].reverse().find((e) => e.type === 'action');
  const actionPending = running && lastAction
    && !events.some((e) => e.type === 'observation' && e.i > lastAction.i);
  const result = terminal?.result || meta?.result;

  const cancel = async () => {
    setCancelling(true);
    try { await api.agentBuildCancel(worldId); } catch { /* build may already be over */ }
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
            {running ? 'Back — the build keeps running' : 'Back'}
          </button>
        )}
        <div className="bg-gray-800/60 border border-gray-700 rounded-xl p-6 space-y-4">

          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="text-2xl font-bold text-gray-100 flex items-center gap-3">
                {running && (
                  <span className="inline-block w-5 h-5 border-2 border-emerald-400/30 border-t-emerald-400 rounded-full animate-spin" />
                )}
                Agent build
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full border ${STATUS_STYLE[status] || 'bg-gray-800 text-gray-400 border-gray-700'}`}>
                  {gone ? 'not found' : (STATUS_LABEL[status] || status)}
                </span>
              </h2>
              <p className="text-gray-500 text-xs mt-1">
                World <span className="text-gray-400">{worldId}</span>
                {' · '}turn {turns}{' · '}{toolCalls} tool calls
              </p>
            </div>
            <div className="flex gap-2 shrink-0">
              {running && !gone && (
                <button
                  onClick={cancel}
                  disabled={cancelling}
                  className="px-3 py-1.5 rounded-lg border border-red-900 text-red-300 hover:bg-red-950/40 disabled:opacity-50 text-sm transition-colors"
                >
                  {cancelling ? 'Cancelling…' : 'Cancel build'}
                </button>
              )}
              {!running && status === 'done' && (
                <button
                  onClick={onOpenWorlds}
                  className="px-3 py-1.5 rounded-lg bg-emerald-700 hover:bg-emerald-600 text-sm font-medium transition-colors"
                >
                  Open world list
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

          {meta?.seed_prompt && (
            <p className="text-sm text-gray-400 border-l-2 border-gray-700 pl-3">{meta.seed_prompt}</p>
          )}

          {meta?.brief?.rules?.length > 0 && (
            <div className="text-xs border-l-2 border-emerald-900 pl-3 space-y-0.5">
              <p className="text-gray-400 font-medium">
                Co-authored rules <span className="text-gray-600">— the build is judged against these</span>
              </p>
              {meta.brief.rules.map((r, i) => (
                <p key={i} className="text-gray-500">• {r}</p>
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
            <div className="text-[11px] uppercase tracking-wider text-gray-600 mb-1">Action log</div>
            <div ref={logRef} className="bg-gray-900/50 border border-gray-800 rounded-lg p-3 max-h-96 overflow-y-auto space-y-1.5">
              {events.length === 0 && (
                <p className="text-xs text-gray-600">{gone ? 'No log.' : 'Waiting for the first turn…'}</p>
              )}
              {events.map((evt) => <LogRow key={evt.i} evt={evt} />)}
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}
