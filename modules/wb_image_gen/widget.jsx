import React, { useState, useEffect, useCallback, useRef } from 'react';

// Shared across every mounted footer instance (the module loader evaluates
// this file once): a single index cache and a single poller, so N messages
// don't fire N request loops.
const API_BASE = '/api/modules/wb_image_gen';
const POLL_MS = 4000;

const store = {
  records: [],
  pending: 0,
  // 'off' | 'blur' | 'blackout' — how finished images appear until clicked.
  conceal: 'off',
  // Record ids the user has revealed this browser session. Shared so a
  // remount (scroll, save switch and back) doesn't re-hide them.
  revealed: new Set(),
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
    store.conceal = data.chat_image_conceal || 'off';
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

// Every image file a finished record owns. Multi-image records (parallel
// generation) carry `filenames`; older single-image records only `filename`.
function recordFiles(record) {
  const names = Array.isArray(record.filenames) && record.filenames.length
    ? record.filenames
    : record.filename ? [record.filename] : [];
  return names.filter(Boolean);
}

// How long the slide-in / snap-back animations run.
const SLIDE_MS = 240;

// Fullscreen viewer over a flat list of finished images ({record, filename}).
// Swipe, the arrow keys, or the on-screen arrows move between images; Esc or
// a click/tap closes. While a touch swipe is in progress the image follows
// the finger (rubber-banding past the ends), and every navigation slides the
// incoming image in from the side it came from. A failed record
// (errorRecord) shows its error instead.
function Lightbox({ items, index, onNavigate, onClose, errorRecord }) {
  const touchRef = useRef(null);
  const swipedRef = useRef(false);
  // Finger-follow offset of the current image, and whether a drag is live
  // (live drags track the finger directly, with no transition).
  const [dragX, setDragX] = useState(0);
  const [dragging, setDragging] = useState(false);
  // Which side the incoming image slides in from after a navigation.
  const [slideFrom, setSlideFrom] = useState(null);
  const count = errorRecord ? 0 : (items ? items.length : 0);
  const safeIndex = Math.max(0, Math.min(index || 0, count - 1));
  const item = count > 0 ? items[safeIndex] : null;

  const go = useCallback((delta) => {
    const next = safeIndex + delta;
    if (next >= 0 && next < count) {
      setSlideFrom(delta > 0 ? 'right' : 'left');
      onNavigate(next);
    }
  }, [safeIndex, count, onNavigate]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') onClose();
      else if (e.key === 'ArrowLeft') go(-1);
      else if (e.key === 'ArrowRight') go(1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [go, onClose]);

  if (!errorRecord && !item) return null;

  const navBtn =
    'absolute top-1/2 -translate-y-1/2 z-10 h-11 w-11 flex items-center justify-center ' +
    'rounded-full bg-black/50 border border-white/10 text-2xl leading-none text-gray-300 ' +
    'hover:bg-black/80 hover:text-white transition-colors cursor-pointer';

  return (
    <div
      className="fixed inset-0 z-50 bg-black/90 flex flex-col items-center justify-center p-4 cursor-zoom-out"
      style={{ touchAction: 'pan-y' }}
      onClick={() => {
        // A horizontal drag fires a click on release; that click must not
        // close the viewer, whether the drag navigated or snapped back.
        if (swipedRef.current) { swipedRef.current = false; return; }
        onClose();
      }}
      onTouchStart={(e) => {
        touchRef.current = {
          x: e.touches[0].clientX, y: e.touches[0].clientY, horizontal: null,
        };
      }}
      onTouchMove={(e) => {
        const start = touchRef.current;
        if (!start || count < 2) return;
        const dx = e.touches[0].clientX - start.x;
        const dy = e.touches[0].clientY - start.y;
        // Decide once, from the first clear movement, whether this touch is a
        // horizontal swipe; vertical gestures never move the image.
        if (start.horizontal === null && (Math.abs(dx) > 6 || Math.abs(dy) > 6)) {
          start.horizontal = Math.abs(dx) > Math.abs(dy);
        }
        if (!start.horizontal) return;
        const atEdge = (dx > 0 && safeIndex === 0) || (dx < 0 && safeIndex === count - 1);
        setDragging(true);
        // Rubber-band: dragging past either end moves at a third the speed.
        setDragX(atEdge ? dx / 3 : dx);
      }}
      onTouchEnd={(e) => {
        const start = touchRef.current;
        touchRef.current = null;
        setDragging(false);
        setDragX(0);
        if (!start || !start.horizontal) return;
        const dx = e.changedTouches[0].clientX - start.x;
        if (Math.abs(dx) > 8) swipedRef.current = true;
        if (Math.abs(dx) > 48) go(dx < 0 ? 1 : -1);
      }}
    >
      <style>{`
        @keyframes wb-lightbox-slide-right {
          from { transform: translateX(64px); opacity: 0.25; }
          to { transform: translateX(0); opacity: 1; }
        }
        @keyframes wb-lightbox-slide-left {
          from { transform: translateX(-64px); opacity: 0.25; }
          to { transform: translateX(0); opacity: 1; }
        }
      `}</style>
      {errorRecord ? (
        <div
          className="max-w-lg w-full rounded-lg border border-red-900/60 bg-gray-900/80 p-6 text-center cursor-auto"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="text-3xl mb-3" aria-hidden="true">🚫</div>
          <p className="text-sm font-semibold text-red-300 mb-2">Image generation failed</p>
          <p className="text-xs text-gray-300 whitespace-pre-wrap break-words">
            {errorRecord.error || 'unknown error'}
          </p>
          {errorRecord.image_prompt && (
            <p className="mt-4 text-[11px] text-gray-500 whitespace-pre-wrap break-words">
              <span className="text-gray-600">Prompt: </span>{errorRecord.image_prompt}
            </p>
          )}
        </div>
      ) : (
        <>
          {count > 1 && (
            <span className="absolute top-4 left-1/2 -translate-x-1/2 px-2.5 py-1 rounded-full bg-black/60 border border-white/10 text-xs text-gray-300">
              {safeIndex + 1} / {count}
            </span>
          )}
          {safeIndex > 0 && (
            <button
              onClick={(e) => { e.stopPropagation(); go(-1); }}
              aria-label="Previous image"
              className={`${navBtn} left-3`}
            >
              ‹
            </button>
          )}
          {safeIndex < count - 1 && (
            <button
              onClick={(e) => { e.stopPropagation(); go(1); }}
              aria-label="Next image"
              className={`${navBtn} right-3`}
            >
              ›
            </button>
          )}
          <img
            key={item.filename}
            src={`${API_BASE}/images/file/${item.filename}`}
            alt={item.prompt || item.record.image_prompt || 'Story illustration'}
            draggable={false}
            onAnimationEnd={() => setSlideFrom(null)}
            style={{
              transform: dragX ? `translateX(${dragX}px)` : undefined,
              // Live drags track the finger; releasing below the swipe
              // threshold animates the snap back to center.
              transition: dragging ? 'none' : `transform ${SLIDE_MS}ms ease`,
              animation: slideFrom && !dragging
                ? `wb-lightbox-slide-${slideFrom} ${SLIDE_MS}ms ease`
                : undefined,
            }}
            className="max-w-full max-h-[85vh] rounded-lg shadow-2xl select-none"
          />
          {(item.prompt || item.record.image_prompt) && (
            <p className="mt-3 max-w-2xl text-center text-xs text-gray-400 line-clamp-4">
              {item.prompt || item.record.image_prompt}
            </p>
          )}
        </>
      )}
    </div>
  );
}

export default function ImageFooter({ state, slotName, message, messageTurn }) {
  const [, setTick] = useState(0);
  const [lightbox, setLightbox] = useState(null);
  const [notice, setNotice] = useState(null);
  // Record id whose delete button is in its "Sure?" confirm state.
  const [armed, setArmed] = useState(null);
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

  // Flat list of every finished image under this message — the lightbox
  // swipes across all of them, spanning multi-image records. Each image in a
  // batch has its own prompt (image_prompts aligns with filenames).
  const galleryItems = records.flatMap((r) =>
    r.status === 'done'
      ? recordFiles(r).map((filename, i) => ({
          record: r,
          filename,
          prompt: (Array.isArray(r.image_prompts) && r.image_prompts[i]) || r.image_prompt,
        }))
      : []);
  const openAt = (filename) =>
    setLightbox({ index: Math.max(0, galleryItems.findIndex((it) => it.filename === filename)) });

  // Retry an error or regenerate a done image: same endpoint, the backend
  // removes the record being replaced. Same prompt, fresh seed.
  const regenerate = async (recordId) => {
    try {
      const res = await fetch(`${API_BASE}/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ retry_record_id: recordId }),
      });
      if (res.status === 409) {
        setNotice('An image is already generating — try again in a moment.');
        setTimeout(() => setNotice(null), 4000);
        return;
      }
      refreshIndex(saveId);
    } catch (e) { /* surfaced by the record staying in error */ }
  };

  // Deleting throws the image file away, so the first tap only arms the
  // button ("Sure?"); it disarms itself after a beat.
  const remove = async (recordId) => {
    if (armed !== recordId) {
      setArmed(recordId);
      setTimeout(() => setArmed((a) => (a === recordId ? null : a)), 3000);
      return;
    }
    setArmed(null);
    try {
      await fetch(`${API_BASE}/images/${recordId}`, { method: 'DELETE' });
      refreshIndex(saveId);
    } catch (e) { /* the next poll reconciles */ }
  };

  const overlayBtn =
    'px-2 py-1 rounded text-xs leading-none bg-black/60 text-gray-300 ' +
    'border border-white/10 hover:bg-black/80 hover:text-white transition-colors';

  const reveal = (recordId) => {
    store.revealed.add(recordId);
    notify();
  };

  return (
    <div className="mt-4 space-y-3">
      {records.map((r) => {
        const files = r.status === 'done' ? recordFiles(r) : [];
        if (files.length > 0) {
          const concealed = store.conceal !== 'off' && !store.revealed.has(r.id);
          // Frame sized from the record's aspect ratio, image fills it: the
          // box exists before the file loads, so nothing shifts on load and
          // the pending-placeholder swap is footprint-identical. Sizing the
          // <img> itself via width/height attributes doesn't work — the
          // specified width plus the max-h cap breaks the aspect ratio and
          // stretches the image. Records without dimensions (old saves) fall
          // back to the image's natural size, as before.
          const hasSize = r.width > 0 && r.height > 0;
          const concealCls = concealed
            ? store.conceal === 'blackout'
              ? 'cursor-pointer brightness-0'
              : 'cursor-pointer blur-2xl scale-110'
            : 'cursor-zoom-in';
          return (
            <div
              key={r.id}
              className={files.length > 1 ? 'relative block max-w-2xl' : 'relative inline-block max-w-full'}
            >
              {/* The wrapper clips the blur's soft edges (the image is scaled
                  up slightly so blurred content still fills its own frame). */}
              {files.length > 1 ? (
                // A parallel batch: two-up grid of equal frames; the lightbox
                // (click any of them) swipes through the full-size versions.
                <div className="grid grid-cols-2 gap-2">
                  {files.map((filename) => (
                    <div
                      key={filename}
                      className="overflow-hidden rounded-lg"
                      style={{ aspectRatio: hasSize ? `${r.width} / ${r.height}` : '1 / 1' }}
                    >
                      <img
                        src={`${API_BASE}/images/file/${filename}`}
                        alt={concealed ? 'Hidden story illustration' : (r.image_prompt || 'Story illustration')}
                        loading="lazy"
                        onClick={() => (concealed ? reveal(r.id) : openAt(filename))}
                        className={`w-full h-full object-cover rounded-lg border border-gray-700/60 shadow-lg ${concealCls}`}
                      />
                    </div>
                  ))}
                </div>
              ) : (
                <div
                  className={`overflow-hidden rounded-lg${hasSize ? ' h-80 max-w-full' : ''}`}
                  style={hasSize ? { aspectRatio: `${r.width} / ${r.height}` } : undefined}
                >
                  <img
                    src={`${API_BASE}/images/file/${files[0]}`}
                    alt={concealed ? 'Hidden story illustration' : (r.image_prompt || 'Story illustration')}
                    loading="lazy"
                    onClick={() => (concealed ? reveal(r.id) : openAt(files[0]))}
                    className={`${hasSize ? 'w-full h-full object-cover' : 'max-h-80 max-w-full'} rounded-lg border border-gray-700/60 shadow-lg ${concealCls}`}
                  />
                </div>
              )}
              {concealed && (
                <button
                  onClick={() => reveal(r.id)}
                  aria-label="Reveal illustration"
                  className="absolute inset-0 flex items-center justify-center rounded-lg cursor-pointer"
                >
                  <span className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-black/60 border border-white/10 text-xs text-gray-300">
                    <span aria-hidden="true">👁</span> Click to reveal
                  </span>
                </button>
              )}
              <div className="absolute top-2 right-2 flex gap-1">
                <button
                  onClick={() => regenerate(r.id)}
                  title={files.length > 1
                    ? 'Regenerate (same prompt, new batch of images)'
                    : 'Regenerate (same prompt, new image)'}
                  aria-label="Regenerate illustration"
                  className={overlayBtn}
                >
                  ↻
                </button>
                <button
                  onClick={() => remove(r.id)}
                  title={files.length > 1
                    ? `Remove these ${files.length} illustrations`
                    : 'Remove this illustration'}
                  aria-label="Remove illustration"
                  className={armed === r.id
                    ? 'px-2 py-1 rounded text-xs leading-none bg-red-900/80 text-red-200 border border-red-500/50'
                    : overlayBtn}
                >
                  {armed === r.id ? 'Sure?' : '✕'}
                </button>
              </div>
            </div>
          );
        }
        if (r.status === 'error') {
          return (
            <div key={r.id} className="flex items-center gap-2 text-xs text-gray-500">
              <button
                onClick={() => setLightbox({ record: r })}
                title="Open to see why it failed"
                className="flex items-center gap-2 min-w-0 text-left hover:text-gray-300 transition-colors"
              >
                <span className="text-red-400/70 shrink-0">🎨 Illustration failed:</span>
                <span className="truncate max-w-xs">{r.error || 'unknown error'}</span>
              </button>
              <button
                onClick={() => regenerate(r.id)}
                className="px-2 py-0.5 rounded bg-gray-800 border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-colors"
              >
                Retry
              </button>
              <button
                onClick={() => remove(r.id)}
                className={armed === r.id
                  ? 'px-2 py-0.5 rounded bg-red-900/80 border border-red-500/50 text-red-200'
                  : 'px-2 py-0.5 rounded bg-gray-800 border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-colors'}
              >
                {armed === r.id ? 'Sure?' : 'Dismiss'}
              </button>
            </div>
          );
        }
        // Reserve the finished image's footprint (same max-h-80 cap, same
        // aspect) while it generates, so the swap to the real image doesn't
        // shift the reader's scroll point.
        return (
          <div
            key={r.id}
            style={{ aspectRatio: r.width > 0 && r.height > 0 ? `${r.width} / ${r.height}` : '1 / 1' }}
            className="h-80 max-w-full rounded-lg border border-gray-700/60 bg-black/20 flex items-center justify-center gap-2"
          >
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-purple-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-purple-500" />
            </span>
            <span className="text-xs text-gray-500 animate-pulse">🎨 Illustrating scene…</span>
          </div>
        );
      })}
      {notice && <p className="text-xs text-amber-400/80">{notice}</p>}
      {lightbox && (
        <Lightbox
          items={galleryItems}
          index={lightbox.index ?? 0}
          errorRecord={lightbox.record || null}
          onNavigate={(i) => setLightbox({ index: i })}
          onClose={() => setLightbox(null)}
        />
      )}
    </div>
  );
}
