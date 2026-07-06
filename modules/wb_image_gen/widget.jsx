import React, { useState, useEffect, useCallback } from 'react';

// Shared across every mounted footer instance (the module loader evaluates
// this file once): a single index cache and a single poller, so N messages
// don't fire N request loops.
const API_BASE = '/api/modules/wb_image_gen';
const POLL_MS = 4000;

const store = {
  records: [],
  pending: 0,
  saveId: null,
  listeners: new Set(),
  timer: null,
  fetching: false,
};

function notify() {
  store.listeners.forEach((fn) => fn());
}

async function refreshIndex(saveId) {
  store.saveId = saveId;
  if (store.fetching) return;
  store.fetching = true;
  try {
    const res = await fetch(`${API_BASE}/images?save_id=${encodeURIComponent(saveId)}`);
    if (!res.ok) return;
    const data = await res.json();
    store.records = data.records || [];
    store.pending = data.pending || 0;
    notify();
  } catch (e) {
    // Network hiccups just mean the next poll tries again.
  } finally {
    store.fetching = false;
  }
  ensurePolling();
}

// Poll only while something is in flight; stop the moment the queue drains.
// The timer always reads the store's current save so save switches are safe.
function ensurePolling() {
  if (store.pending > 0 && store.listeners.size > 0) {
    if (!store.timer) {
      store.timer = setInterval(() => refreshIndex(store.saveId), POLL_MS);
    }
  } else if (store.timer) {
    clearInterval(store.timer);
    store.timer = null;
  }
}

function Lightbox({ record, onClose }) {
  return (
    <div
      className="fixed inset-0 z-50 bg-black/90 flex flex-col items-center justify-center p-4 cursor-zoom-out"
      onClick={onClose}
    >
      <img
        src={`${API_BASE}/images/file/${record.filename}`}
        alt={record.image_prompt || 'Story illustration'}
        className="max-w-full max-h-[85vh] rounded-lg shadow-2xl"
      />
      {record.image_prompt && (
        <p className="mt-3 max-w-2xl text-center text-xs text-gray-400 line-clamp-4">
          {record.image_prompt}
        </p>
      )}
    </div>
  );
}

export default function ImageFooter({ state, slotName, message, messageTurn }) {
  const [, setTick] = useState(0);
  const [lightbox, setLightbox] = useState(null);
  const saveId = state?.active_save_id;
  const lastTrigger = state?.module_data?.wb_image_gen?.last_trigger;

  const rerender = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!saveId) return undefined;
    store.listeners.add(rerender);
    // Fresh save, new trigger, or turn advance: refetch the index once; the
    // shared poller takes over only while something is pending.
    refreshIndex(saveId);
    return () => {
      store.listeners.delete(rerender);
      ensurePolling();
    };
  }, [saveId, lastTrigger, state?.turn, rerender]);

  if (slotName !== 'slot_message_footer') return null;
  if (!saveId) return null;
  if (!message || message.role !== 'assistant' || message.error) return null;

  // Match by content, not turn: every record stores the exact start of the
  // narration it illustrates, and message text is that narration verbatim.
  // Turn numbers get reused (reswipe, undo) and drift while a turn is still
  // post-processing, so they only serve as a fallback for excerpt-less
  // records (e.g. studio prompts fired into a save).
  const records = store.records.filter((r) => {
    if (r.save_id !== saveId) return false;
    const excerpt = r.narration_excerpt || '';
    if (excerpt) return (message.content || '').startsWith(excerpt);
    return messageTurn != null && r.turn === messageTurn;
  });
  if (records.length === 0) return null;

  const retry = async (recordId) => {
    try {
      await fetch(`${API_BASE}/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ retry_record_id: recordId }),
      });
      refreshIndex(saveId);
    } catch (e) { /* surfaced by the record staying in error */ }
  };

  return (
    <div className="mt-4 space-y-3">
      {records.map((r) => {
        if (r.status === 'done' && r.filename) {
          return (
            <div key={r.id}>
              <img
                src={`${API_BASE}/images/file/${r.filename}`}
                alt={r.image_prompt || 'Story illustration'}
                loading="lazy"
                onClick={() => setLightbox(r)}
                className="max-h-80 rounded-lg border border-gray-700/60 shadow-lg cursor-zoom-in"
              />
            </div>
          );
        }
        if (r.status === 'error') {
          return (
            <div key={r.id} className="flex items-center gap-2 text-xs text-gray-500">
              <span className="text-red-400/70">🎨 Illustration failed:</span>
              <span className="truncate max-w-xs" title={r.error || ''}>{r.error || 'unknown error'}</span>
              <button
                onClick={() => retry(r.id)}
                className="px-2 py-0.5 rounded bg-gray-800 border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-colors"
              >
                Retry
              </button>
            </div>
          );
        }
        return (
          <div key={r.id} className="flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-purple-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-purple-500" />
            </span>
            <span className="text-xs text-gray-500 animate-pulse">🎨 Illustrating scene…</span>
          </div>
        );
      })}
      {lightbox && <Lightbox record={lightbox} onClose={() => setLightbox(null)} />}
    </div>
  );
}
