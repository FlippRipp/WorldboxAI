import React, { useState, useEffect, useCallback, useRef } from 'react';

const API_BASE = '/api/modules/wb_image_gen';

const inputCls =
  'w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 ' +
  'focus:border-purple-500 focus:outline-none placeholder-gray-600';

// Mirrors LORA_WEIGHT_MIN/MAX in the backend.
const LORA_WEIGHT_MIN = -10;
const LORA_WEIGHT_MAX = 10;

// What the per-image AI pass decides for a LoRA; mirrors the backend's
// LORA_LLM_MODES and its legacy-field fallback.
const LORA_LLM_MODES = ['off', 'gate', 'weight', 'both'];
const LORA_LLM_MODE_LABELS = {
  off: 'AI: off', gate: 'AI: gated', weight: 'AI: weight', both: 'AI: both',
};

function loraLlmMode(entry) {
  if (LORA_LLM_MODES.includes(entry.llm_mode)) return entry.llm_mode;
  const hasText = (entry.condition || '').trim().length > 0;
  if (entry.llm_weight) return hasText ? 'both' : 'weight';
  return hasText ? 'gate' : 'off';
}
const labelCls = 'block text-xs uppercase tracking-wider text-gray-500 mb-1.5';
const sectionCls = 'bg-gray-900/60 border border-gray-800 rounded-xl p-5 space-y-4';

// Textarea that grows with its content (rows sets the minimum height), so
// longer text stays readable while typing. Modeled on the CharacterBuilder's
// AutoTextarea. With height reset to auto the rows attribute drives the
// rendered height, and scrollHeight is never below it — so rows acts as the
// floor the field shrinks back to.
function AutoGrowTextarea({ value, onChange, onBlur, onKeyDown, placeholder, className, rows = 1 }) {
  const ref = useRef(null);
  const adjustHeight = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }, []);
  useEffect(() => { adjustHeight(); }, [value, adjustHeight]);
  return (
    <textarea
      ref={ref}
      value={value}
      onChange={onChange}
      onInput={adjustHeight}
      onBlur={onBlur}
      onKeyDown={onKeyDown}
      placeholder={placeholder}
      rows={rows}
      className={`${className} resize-none overflow-hidden whitespace-pre-wrap break-words`}
    />
  );
}

const TABS = [
  { id: 'setup', label: 'Setup' },
  { id: 'output', label: 'Output' },
  { id: 'prompting', label: 'Prompting' },
  { id: 'loras', label: 'LoRAs' },
  { id: 'generate', label: 'Generate' },
  { id: 'library', label: 'Image Library' },
];

function Toggle({ checked, onChange, label }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className="flex items-center gap-3 text-sm text-gray-300"
    >
      <span
        className={`relative inline-flex h-5 w-9 shrink-0 rounded-full transition-colors ${
          checked ? 'bg-purple-600' : 'bg-gray-700'
        }`}
      >
        <span
          className="absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform left-0.5"
          style={{ transform: checked ? 'translateX(16px)' : 'translateX(0)' }}
        />
      </span>
      {label}
    </button>
  );
}

function StatusBadge({ status }) {
  const styles = {
    done: 'bg-green-900/50 text-green-300 border-green-800',
    error: 'bg-red-900/50 text-red-300 border-red-800',
    pending: 'bg-yellow-900/40 text-yellow-300 border-yellow-800 animate-pulse',
    prompting: 'bg-purple-900/40 text-purple-300 border-purple-800 animate-pulse',
    generating: 'bg-purple-900/40 text-purple-300 border-purple-800 animate-pulse',
  };
  return (
    <span className={`px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider border ${styles[status] || 'bg-gray-800 text-gray-400 border-gray-700'}`}>
      {status}
    </span>
  );
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
            alt={item.prompt || item.record.image_prompt || 'Generated image'}
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
            <p className="mt-3 max-w-2xl text-center text-xs text-gray-400">
              {item.prompt || item.record.image_prompt}
            </p>
          )}
        </>
      )}
    </div>
  );
}

// Searchable dropdown over Novita's checkpoint catalog (thousands of models).
// Searches server-side via the module's /models proxy; cursor-paginated. The
// proxy may answer a spaced query with a catalog-style respelling (Novita
// names Civitai mirrors after the no-space file name) — pagination must reuse
// that effective_query, not what the user typed.
// onSelect receives the whole model object (sd_name + base_model metadata).
function ModelPicker({ value, valueBase, hasKey, local, onSelect }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [models, setModels] = useState([]);
  const [nextCursor, setNextCursor] = useState('');
  const [effQuery, setEffQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const debounceRef = useRef(null);
  const seqRef = useRef(0);
  const boxRef = useRef(null);
  const enabled = local || hasKey;   // the local WebUI needs no API key

  const search = useCallback(async (q, cursor = '') => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError('');
    try {
      const params = new URLSearchParams({ query: q });
      if (cursor) params.set('cursor', cursor);
      const res = await fetch(`${API_BASE}/models?${params}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (seq !== seqRef.current) return; // stale response, a newer search is in flight
      setModels((prev) => (cursor ? [...prev, ...data.models] : data.models));
      setNextCursor(data.next_cursor || '');
      if (!cursor) setEffQuery(data.effective_query || '');
    } catch (e) {
      if (seq === seqRef.current) setError(String(e.message || e));
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, []);

  // Debounced search while the dropdown is open.
  useEffect(() => {
    if (!open) return undefined;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(query), 350);
    return () => clearTimeout(debounceRef.current);
  }, [open, query, search]);

  // Close on outside click.
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  return (
    <div ref={boxRef} className="relative">
      <label className={labelCls}>Model</label>
      {value && !open && (
        <div className="flex items-center justify-between gap-2 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2">
          <span className="text-sm text-gray-200 font-mono truncate" title={value}>{value}</span>
          <div className="flex items-center gap-2 shrink-0">
            {valueBase && (
              <span className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider bg-gray-800 border border-gray-700 text-gray-400">
                {valueBase}
              </span>
            )}
            <button
              onClick={() => setOpen(true)}
              disabled={!enabled}
              className="text-xs text-purple-400 hover:text-purple-300 disabled:opacity-40"
            >
              Change
            </button>
          </div>
        </div>
      )}
      {(!value || open) && (
        <input
          type="text"
          value={query}
          autoFocus={open}
          disabled={!enabled}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false); }}
          placeholder={local
            ? 'Search your installed checkpoints…'
            : hasKey ? 'Search thousands of models — e.g. realistic, anime, fantasy…' : 'Save an API key first'}
          className={inputCls}
        />
      )}
      {!enabled && !value && (
        <p className="text-xs text-yellow-500 mt-1">Save an API key to browse models.</p>
      )}

      {open && (
        <div className="absolute z-30 mt-1 w-full max-h-80 overflow-y-auto bg-gray-900 border border-gray-700 rounded-lg shadow-2xl">
          {error && <div className="px-3 py-2 text-xs text-red-400">{error}</div>}
          {!error && models.length === 0 && !loading && (
            <div className="px-3 py-2 text-xs text-gray-500 italic">
              {local
                ? 'No checkpoints found — is the WebUI running with --api?'
                : 'No models found.'}
            </div>
          )}
          {local && !loading && (
            <button
              onClick={async () => {
                setRefreshing(true);
                try {
                  await fetch(`${API_BASE}/local/refresh`, { method: 'POST' });
                } catch (e) { /* the re-search below surfaces errors */ }
                setRefreshing(false);
                search(query);
              }}
              disabled={refreshing}
              className="w-full px-3 py-1.5 text-left text-[11px] text-purple-400 hover:text-purple-300 hover:bg-gray-800 border-b border-gray-800 disabled:opacity-40"
            >
              {refreshing ? 'Rescanning model folders…' : '↻ Rescan the WebUI’s model folders'}
            </button>
          )}
          {!error && models.length > 0 && effQuery && effQuery !== query.trim() && (
            <div className="px-3 py-1.5 text-[10px] text-gray-500 border-b border-gray-800">
              Matched catalog spelling <span className="font-mono text-gray-400">{effQuery}</span>
            </div>
          )}
          {models.map((m) => (
            <button
              key={m.sd_name}
              onClick={() => { onSelect(m); setOpen(false); setQuery(''); }}
              className={`w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-gray-800 transition-colors ${
                m.sd_name === value ? 'bg-purple-900/30' : ''
              }`}
            >
              {m.cover_url ? (
                <img src={m.cover_url} alt="" loading="lazy" className="w-9 h-9 rounded object-cover shrink-0 bg-gray-800" />
              ) : (
                <div className="w-9 h-9 rounded bg-gray-800 shrink-0" />
              )}
              <div className="min-w-0 flex-1">
                <div className="text-sm text-gray-200 truncate">{m.name || m.sd_name}</div>
                <div className="text-[10px] text-gray-500 font-mono truncate">{m.sd_name}</div>
              </div>
              {(m.is_sdxl || m.base_model) && (
                <span className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider bg-gray-800 border border-gray-700 text-gray-400 shrink-0">
                  {m.is_sdxl ? 'SDXL' : m.base_model}
                </span>
              )}
            </button>
          ))}
          {loading && <div className="px-3 py-2 text-xs text-gray-500 animate-pulse">Searching…</div>}
          {!loading && nextCursor && (
            <button
              onClick={() => search(effQuery || query, nextCursor)}
              className="w-full px-3 py-2 text-xs text-purple-400 hover:text-purple-300 hover:bg-gray-800 transition-colors"
            >
              Load more…
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// API-key input with its own submit: the key is validated against the
// provider before it is stored, independent of the main Save button.
function KeyField({ provider, label, placeholder, saved, savedMask, onSaved, children }) {
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(null); // { ok, msg }

  const submit = async () => {
    if (!value.trim() || busy) return;
    setBusy(true);
    setStatus(null);
    try {
      const res = await fetch(`${API_BASE}/keys/${provider}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: value.trim() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      onSaved(data);
      setValue('');
      setStatus({ ok: true, msg: 'Key is valid — saved ✓' });
    } catch (e) {
      setStatus({ ok: false, msg: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <label className={labelCls}>{label}</label>
      <div className="flex gap-2">
        <input
          type="password"
          value={value}
          onChange={(e) => { setValue(e.target.value); setStatus(null); }}
          onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
          placeholder={saved ? `Saved (${savedMask}) — type to replace` : placeholder}
          className={inputCls}
          autoComplete="off"
        />
        <button
          onClick={submit}
          disabled={busy || !value.trim()}
          className="px-4 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
        >
          {busy ? 'Checking…' : 'Submit'}
        </button>
      </div>
      {status && (
        <p className={`text-xs mt-1 ${status.ok ? 'text-green-400' : 'text-red-400'}`}>{status.msg}</p>
      )}
      {children}
    </div>
  );
}

// Connection settings for a local A1111/Forge/reForge/SD.Next WebUI. The
// fields ride the normal draft/save flow; Test connection checks the SAVED
// config server-side, so unsaved edits get a save-first hint instead.
function LocalConnectionCard({ config, draft, set }) {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  const unsaved = (draft.local_base_url || '') !== (config.local_base_url || '') ||
    (draft.local_auth_user || '') !== (config.local_auth_user || '') ||
    (draft.local_auth_pass || '') !== (config.local_auth_pass || '');

  const test = async () => {
    setBusy(true);
    setStatus(null);
    try {
      const res = await fetch(`${API_BASE}/local/status`);
      setStatus(await res.json());
    } catch (e) {
      setStatus({ ok: false, error: String(e.message || e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div>
        <label className={labelCls}>Local WebUI address</label>
        <div className="flex gap-2">
          <input
            type="text"
            value={draft.local_base_url ?? ''}
            onChange={(e) => set('local_base_url', e.target.value)}
            placeholder="http://127.0.0.1:7860"
            className={inputCls}
            autoComplete="off"
          />
          <button
            onClick={test}
            disabled={busy}
            className="px-4 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium disabled:opacity-40 shrink-0"
          >
            {busy ? 'Checking…' : 'Test connection'}
          </button>
        </div>
        {status && (
          <p className={`text-xs mt-1 ${status.ok ? 'text-green-400' : 'text-red-400'}`}>
            {status.ok
              ? `Connected ✓ — ${status.checkpoint_count} checkpoint${status.checkpoint_count === 1 ? '' : 's'} installed${status.current_checkpoint ? `, ${status.current_checkpoint} loaded` : ''}`
              : status.error}
          </p>
        )}
        {status && status.helper && (
          <p className={`text-xs mt-1 ${status.helper.ok ? 'text-green-400' : 'text-red-400'}`}>
            {status.helper.ok
              ? 'Install helper ✓ — remote installs and installed-model badges enabled'
              : `Install helper: ${status.helper.error || 'not reachable'}`}
          </p>
        )}
        {unsaved && (
          <p className="text-xs text-yellow-600 mt-1">Unsaved connection changes — press Save Changes before testing.</p>
        )}
        <p className="text-xs text-gray-600 mt-1">
          Any AUTOMATIC1111-compatible WebUI (A1111, Forge, reForge, SD.Next) started with the{' '}
          <span className="font-mono text-gray-500">--api</span> flag.{' '}
          <button onClick={() => setShowGuide((s) => !s)} className="text-purple-400 hover:underline">
            {showGuide ? 'Hide quick start' : 'How do I set it up?'}
          </button>
        </p>
        {showGuide && (
          <ol className="mt-2 space-y-1.5 text-xs text-gray-400 list-decimal list-inside bg-gray-950/60 border border-gray-800 rounded-lg p-3">
            <li>
              Install a WebUI —{' '}
              <a href="https://github.com/AUTOMATIC1111/stable-diffusion-webui" target="_blank" rel="noreferrer" className="text-purple-400 hover:underline">
                AUTOMATIC1111
              </a>{' '}
              or{' '}
              <a href="https://github.com/lllyasviel/stable-diffusion-webui-forge" target="_blank" rel="noreferrer" className="text-purple-400 hover:underline">
                Forge
              </a>{' '}
              (faster, also runs Flux) — and drop at least one checkpoint into{' '}
              <span className="font-mono">models/Stable-diffusion</span>.
            </li>
            <li>
              Add <span className="font-mono text-gray-300">--api</span> to its launch flags
              (in <span className="font-mono">webui-user.bat</span>: <span className="font-mono">set COMMANDLINE_ARGS=--api</span>) and start it.
            </li>
            <li>
              Point the address above at it (the default is right for a WebUI on this machine),
              press <span className="text-gray-300 font-medium">Save Changes</span>, then <span className="text-gray-300 font-medium">Test connection</span>.
            </li>
            <li>
              Generation is free and private — images never leave your machine. Only optional
              WebUI logins (<span className="font-mono">--api-auth</span>) need the fields below.
            </li>
          </ol>
        )}
      </div>
      <div className="grid sm:grid-cols-2 gap-3">
        <div>
          <label className={labelCls}>API auth user (optional)</label>
          <input
            type="text"
            value={draft.local_auth_user ?? ''}
            onChange={(e) => set('local_auth_user', e.target.value)}
            placeholder="only with --api-auth"
            className={inputCls}
            autoComplete="off"
          />
        </div>
        <div>
          <label className={labelCls}>API auth password</label>
          <input
            type="password"
            value={draft.local_auth_pass ?? ''}
            onChange={(e) => set('local_auth_pass', e.target.value)}
            placeholder="only with --api-auth"
            className={inputCls}
            autoComplete="off"
          />
        </div>
      </div>
      <div>
        <label className={labelCls}>LoRA folder (optional — enables one-click installs)</label>
        <input
          type="text"
          value={draft.local_lora_dir ?? ''}
          onChange={(e) => set('local_lora_dir', e.target.value)}
          placeholder="e.g. C:\\stable-diffusion-webui\\models\\Lora"
          className={inputCls}
          autoComplete="off"
        />
        <p className="text-xs text-gray-600 mt-1">
          The WebUI's <span className="font-mono">models/Lora</span> folder on this machine. With it set,
          the LoRA browser installs files for you and links them up automatically.
        </p>
      </div>
      <div>
        <label className={labelCls}>Checkpoint folder (optional)</label>
        <input
          type="text"
          value={draft.local_checkpoint_dir ?? ''}
          onChange={(e) => set('local_checkpoint_dir', e.target.value)}
          placeholder="e.g. C:\\stable-diffusion-webui\\models\\Stable-diffusion"
          className={inputCls}
          autoComplete="off"
        />
        <p className="text-xs text-gray-600 mt-1">
          The WebUI's <span className="font-mono">models/Stable-diffusion</span> folder, for one-click
          checkpoint installs. Leave both folders empty when the WebUI runs on another machine (unless
          they are mounted here) and use the install helper below instead.
        </p>
      </div>
      <div className="grid sm:grid-cols-2 gap-3">
        <div>
          <label className={labelCls}>Install helper (for a WebUI on another machine)</label>
          <input
            type="text"
            value={draft.local_helper_url ?? ''}
            onChange={(e) => set('local_helper_url', e.target.value)}
            placeholder="e.g. http://192.168.1.20:7861"
            className={inputCls}
            autoComplete="off"
          />
        </div>
        <div>
          <label className={labelCls}>Helper token (optional)</label>
          <input
            type="password"
            value={draft.local_helper_token ?? ''}
            onChange={(e) => set('local_helper_token', e.target.value)}
            placeholder="only if WB_HELPER_TOKEN is set"
            className={inputCls}
            autoComplete="off"
          />
        </div>
      </div>
      <p className="text-xs text-gray-600 -mt-2">
        The bundled <span className="font-mono">image_server</span> script starts this companion server
        next to the WebUI (port 7861) and prints its address. With it set, the model and LoRA browsers
        install files onto that machine with live progress bars, and installed models are badged from
        its exact file hashes. Not needed when the WebUI runs on this machine.
      </p>
    </div>
  );
}

// Named per-model setups. Everything checkpoint-specific (model, output,
// prompting, LoRA on/off + weights) lives in the active profile; API keys and
// behavior settings are shared. Switching/creating/deleting all round-trip
// through the backend and hand back the full effective config via onApply.
function ProfileBar({ config, dirty, onApply, onError }) {
  const [busy, setBusy] = useState(false);
  const profiles = config.profiles || [];
  const current = profiles.find((p) => p.id === config.active_profile);

  const call = async (path, options = {}) => {
    setBusy(true);
    onError('');
    try {
      const res = await fetch(`${API_BASE}${path}`, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      onApply(data);
    } catch (e) {
      onError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  // Switching (and create/duplicate, which activate the new profile) replaces
  // the whole settings draft, so unsaved edits need an explicit go-ahead.
  const confirmDiscard = () =>
    !dirty || window.confirm('Discard unsaved changes and switch profile?');

  const switchTo = (id) => {
    if (id === config.active_profile || !confirmDiscard()) return;
    call(`/profiles/${id}/activate`, { method: 'POST' });
  };
  const create = (duplicate) => {
    const suggestion = duplicate ? `${current?.name || 'Profile'} copy` : '';
    const name = window.prompt(
      duplicate ? 'Name for the duplicated profile:' : 'New profile name:', suggestion);
    if (name == null || !name.trim() || !confirmDiscard()) return;
    call('/profiles', {
      method: 'POST',
      body: JSON.stringify({
        name: name.trim(),
        duplicate_from: duplicate ? config.active_profile : null,
      }),
    });
  };
  const rename = () => {
    const name = window.prompt('Rename profile:', current?.name || '');
    if (name == null || !name.trim() || name.trim() === current?.name) return;
    call(`/profiles/${config.active_profile}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: name.trim() }),
    });
  };
  const remove = () => {
    if (!window.confirm(`Delete profile "${current?.name}"? Its model, settings and LoRA states are lost. The LoRA library itself is shared and stays.`)) return;
    call(`/profiles/${config.active_profile}`, { method: 'DELETE' });
  };

  const btnCls = 'px-2.5 py-1.5 rounded-lg bg-gray-900 border border-gray-800 text-xs text-gray-400 hover:text-gray-200 transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0';
  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl px-4 py-3 flex items-center gap-2 flex-wrap">
      <span className="text-xs uppercase tracking-wide text-gray-500 shrink-0">Profile</span>
      <select
        value={config.active_profile || ''}
        onChange={(e) => switchTo(e.target.value)}
        disabled={busy}
        className="bg-gray-900 border border-gray-800 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-purple-500 min-w-[10rem]"
      >
        {profiles.map((p) => (
          <option key={p.id} value={p.id}>{p.name}</option>
        ))}
      </select>
      <div className="flex gap-1.5 flex-wrap">
        <button onClick={() => create(false)} disabled={busy} className={btnCls}>+ New</button>
        <button onClick={() => create(true)} disabled={busy} className={btnCls}>Duplicate</button>
        <button onClick={rename} disabled={busy} className={btnCls}>Rename</button>
        {profiles.length > 1 && (
          <button onClick={remove} disabled={busy} className={`${btnCls} hover:text-red-300`}>
            Delete
          </button>
        )}
      </div>
    </div>
  );
}

// Mirror of the backend's _base_family heuristic.
function baseFamily(base) {
  const ident = String(base || '').toLowerCase();
  if (ident.includes('flux')) return 'flux';
  if (ident.includes('xl') || ident.includes('pony') || ident.includes('illustrious') || ident.includes('noob')) return 'sdxl';
  if (ident.includes('1.5') || ident.includes('sd 1') || ident.includes('sd1')) return 'sd15';
  return '';
}

const FAMILY_LABELS = { flux: 'FLUX.2', sdxl: 'SDXL-class', sd15: 'SD 1.5' };

function fmtCount(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

// Source page link for a browse item or saved entry. Civitai models are always
// routed to the .red domain (same /models/{id} path), regardless of NSFW
// status, so links never bounce off civitai.com's content gating.
function loraLink(item) {
  const url = item.page_url || item.civitai_url || '';
  if (url.startsWith('https://civitai.com/')) {
    return url.replace('https://civitai.com/', 'https://civitai.red/');
  }
  return url;
}

// Availability of a saved LoRA. Local mode: it must be linked to a file
// installed in the WebUI (any family — Forge runs Flux LoRAs through the same
// prompt syntax). Novita: Flux LoRAs travel as download links, SD-family ones
// must exist in Novita's mirrored catalog (or be console-uploaded and named
// manually).
function loraAvailability(entry, isLocal) {
  if (isLocal) {
    const name = entry.local && entry.local.name;
    return name
      ? { ok: true, label: `→ ${name}`, cls: 'bg-green-900/50 text-green-300 border-green-800' }
      : { ok: false, label: 'not installed', cls: 'bg-red-900/50 text-red-300 border-red-800' };
  }
  if (baseFamily(entry.base_model) === 'flux') {
    return entry.download_url
      ? { ok: true, label: 'via link', cls: 'bg-purple-900/40 text-purple-300 border-purple-800' }
      : { ok: false, label: 'no download url', cls: 'bg-red-900/50 text-red-300 border-red-800' };
  }
  if (entry.sd_name_override) {
    return { ok: true, label: 'manual name', cls: 'bg-green-900/50 text-green-300 border-green-800' };
  }
  if (entry.novita && entry.novita.sd_name_in_api) {
    return { ok: true, label: 'on Novita', cls: 'bg-green-900/50 text-green-300 border-green-800' };
  }
  return { ok: false, label: 'not on Novita', cls: 'bg-red-900/50 text-red-300 border-red-800' };
}

// Queried Civitai searches come back relevance-ordered and the proxy sorts
// each request's batch within itself only — so after "Load more" the merged
// list must be re-sorted client-side or the counts jump back up mid-list.
const BROWSE_SORT_KEYS = {
  'Most Downloaded': (i) => i.stats?.downloads || 0,
  'Highest Rated': (i) => i.stats?.likes || 0,
  'Most Liked': (i) => i.stats?.likes || 0,
  'Newest': (i) => i.published_at || '',
  'Recently Updated': (i) => i.published_at || '',
};

function mergeBrowseResults(prev, incoming, sortLabel) {
  const seen = new Set(prev.map((i) => i.id));
  const merged = [...prev, ...incoming.filter((i) => !seen.has(i.id))];
  const key = BROWSE_SORT_KEYS[sortLabel];
  if (key) merged.sort((a, b) => (key(a) < key(b) ? 1 : key(a) > key(b) ? -1 : 0));
  return merged;
}

// Availability badge for a browse result (not yet saved). Local mode checks
// each result's hashes against a scan of the LoRA folder; Novita mode against
// Novita's mirrored catalog (flux LoRAs travel as download links there).
function browseAvailability(item, isLocal) {
  if (item.gated) {
    return {
      label: 'gated', cls: 'bg-yellow-900/40 text-yellow-400 border-yellow-800',
      title: isLocal
        ? 'Gated Hugging Face repo — it cannot be downloaded automatically'
        : 'Gated Hugging Face repo — Novita cannot download it, so it cannot be used',
    };
  }
  if (isLocal) {
    if (item.local_available === true) {
      return {
        label: 'installed', cls: 'bg-green-900/50 text-green-300 border-green-800',
        title: item.local_name ? `In your LoRA folder as ${item.local_name}` : 'In your LoRA folder',
      };
    }
    if (item.local_available === false) {
      return {
        label: 'not installed', cls: 'bg-gray-800/80 text-gray-400 border-gray-700',
        title: 'Not in your LoRA folder yet — press Install',
      };
    }
    return {
      label: 'installed: ?', cls: 'bg-gray-900/40 text-gray-600 border-gray-800',
      title: 'Local availability unknown (set your LoRA folder or the install helper in Setup and it will be matched)',
    };
  }
  if (baseFamily(item.base_model) === 'flux') {
    return item.download_url
      ? { label: 'via link', cls: 'bg-purple-900/40 text-purple-300 border-purple-800', title: 'Sent to FLUX.2 as a download link — no mirror needed' }
      : null;
  }
  if (item.novita_available === true) {
    return {
      label: 'on Novita', cls: 'bg-green-900/50 text-green-300 border-green-800',
      title: item.novita_sd_name ? `In Novita's catalog as ${item.novita_sd_name}` : "In Novita's catalog",
    };
  }
  if (item.novita_available === false) {
    return {
      label: 'not on Novita', cls: 'bg-gray-800/80 text-gray-400 border-gray-700',
      title: "Not in Novita's mirrored catalog — you can still save it and upload it to your own Novita account",
    };
  }
  return {
    label: 'Novita: ?', cls: 'bg-gray-900/40 text-gray-600 border-gray-800',
    title: 'Novita availability unknown (catalog index not built yet)',
  };
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1 << 30) return `${(n / (1 << 30)).toFixed(2)} GB`;
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(0)} MB`;
  return `${(n / 1024).toFixed(0)} kB`;
}

// Progress bar for one in-flight (or just-finished) install.
function InstallProgress({ download, onCancel }) {
  if (!download) return null;
  if (download.status === 'error') {
    return <p className="text-[11px] text-red-400">Install failed: {download.error}</p>;
  }
  if (download.status === 'done') {
    return <p className="text-[11px] text-green-400">Installed ✓ {download.filename}</p>;
  }
  const pct = download.total_bytes
    ? Math.min(100, Math.round((download.received_bytes / download.total_bytes) * 100))
    : null;
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-[10px] text-gray-500">
        <span className="truncate">
          Installing {download.label || download.filename}… {fmtBytes(download.received_bytes)}
          {download.total_bytes ? ` of ${fmtBytes(download.total_bytes)}` : ''}
        </span>
        {onCancel && (
          <button onClick={() => onCancel(download.id)} className="text-gray-600 hover:text-red-400 shrink-0 ml-2">
            cancel
          </button>
        )}
      </div>
      <div className="h-1.5 rounded bg-gray-800 overflow-hidden">
        <div
          className={`h-full bg-purple-500 ${pct == null ? 'w-1/3 animate-pulse' : ''}`}
          style={pct == null ? undefined : { width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// Copy text to the clipboard. navigator.clipboard needs a secure context,
// which a LAN-served http:// app doesn't have — fall back to the legacy
// textarea trick, and report failure so the caller can show the text instead.
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch (e2) {
      return false;
    }
  }
}

// The Install button's version picker: Civitai models ship many versions but
// browse hits carry only the latest, so picking fetches the full list. HF
// items have no versions and install directly (the button skips this panel).
// actionLabel renames the per-version button when the parent's onInstall
// does something other than a one-click install (e.g. copy the link).
function VersionPicker({ item, onInstall, onClose, actionLabel = 'Install' }) {
  const [versions, setVersions] = useState(null);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/civitai/model-versions/${item.model_id}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
        if (!cancelled) setVersions(data.versions || []);
      } catch (e) {
        if (!cancelled) setError(String(e.message || e));
      }
    })();
    return () => { cancelled = true; };
  }, [item.model_id]);

  return (
    <div className="space-y-1.5 border-t border-gray-800 pt-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wider text-gray-500">Pick a version</span>
        <button onClick={onClose} className="text-[10px] text-gray-600 hover:text-gray-300">close</button>
      </div>
      {error && <p className="text-[11px] text-red-400">{error}</p>}
      {versions === null && !error && (
        <p className="text-[11px] text-gray-500 animate-pulse">Loading versions…</p>
      )}
      {(versions || []).map((v) => (
        <div key={v.id} className="flex items-center gap-2 text-[11px]">
          <span className="text-gray-300 truncate flex-1" title={`${v.version_name} · ${v.base_model}`}>
            {v.version_name || v.id}
            <span className="text-gray-600">
              {' '}· {v.base_model}
              {v.size_kb ? ` · ${(v.size_kb / 1024).toFixed(0)} MB` : ''}
            </span>
          </span>
          {v.local_available === true ? (
            <span className="text-green-400 shrink-0">installed ✓</span>
          ) : (
            <button
              onClick={async () => { setBusyId(v.id); await onInstall(v); setBusyId(null); }}
              disabled={busyId !== null}
              className="px-2 py-0.5 rounded bg-purple-600 hover:bg-purple-500 text-white disabled:opacity-40 shrink-0"
            >
              {busyId === v.id ? '…' : actionLabel}
            </button>
          )}
        </div>
      ))}
      {versions !== null && versions.length === 0 && !error && (
        <p className="text-[11px] text-gray-600 italic">No downloadable versions.</p>
      )}
    </div>
  );
}

function LoraRow({ entry, checkpointFamily, provider, canInstall, download, onInstall, onCancelInstall, installedLoras, loadInstalledLoras, onPatch, onDelete, onRematch, myLoras, maxSlots, loadMyUploads, fetchMyUploads }) {
  const [strength, setStrength] = useState(entry.strength ?? 0.7);
  const [showOverride, setShowOverride] = useState(false);
  const [override, setOverride] = useState(entry.sd_name_override || '');
  const [busy, setBusy] = useState(false);
  const [detected, setDetected] = useState(null); // new upload seen but still processing
  const [condition, setCondition] = useState(entry.condition || '');
  const [triggers, setTriggers] = useState((entry.trained_words || []).join(', '));
  const [showLink, setShowLink] = useState(false);
  const [linkFilter, setLinkFilter] = useState('');
  const isLocal = provider === 'local';

  useEffect(() => { setStrength(entry.strength ?? 0.7); }, [entry.strength]);
  useEffect(() => { setCondition(entry.condition || ''); }, [entry.condition]);
  useEffect(() => { setTriggers((entry.trained_words || []).join(', ')); },
    [entry.trained_words]);

  const llmMode = loraLlmMode(entry);
  const aiWeighted = llmMode === 'weight' || llmMode === 'both';

  const commitCondition = () => {
    const next = condition.trim();
    if ((entry.condition || '') !== next) {
      // Typing a condition on a mode-less LoRA gates it, matching the old
      // "condition text means gated" behavior.
      const patch = { condition: next };
      if (next && llmMode === 'off') patch.llm_mode = 'gate';
      onPatch(entry.id, patch);
    }
  };

  const commitTriggers = () => {
    const next = triggers.split(',').map((w) => w.trim()).filter(Boolean).slice(0, 20);
    const cur = entry.trained_words || [];
    if (next.join('\n') !== cur.join('\n')) onPatch(entry.id, { trained_words: next });
  };

  const cycleLlmMode = () => {
    const next = LORA_LLM_MODES[(LORA_LLM_MODES.indexOf(llmMode) + 1) % LORA_LLM_MODES.length];
    onPatch(entry.id, { llm_mode: next });
  };

  const commitStrength = () => {
    const v = Number(strength);
    if (!Number.isFinite(v)) {
      setStrength(entry.strength ?? 0.7);
      return;
    }
    const clamped = Math.max(LORA_WEIGHT_MIN, Math.min(LORA_WEIGHT_MAX, v));
    setStrength(clamped);
    if (clamped !== (entry.strength ?? 0.7)) onPatch(entry.id, { strength: clamped });
  };

  // While the upload helper is open, watch the account's private uploads:
  // anything that appears after opening is assumed to be the file the user
  // just pushed through the console, and is linked to this entry the moment
  // Novita finishes processing it. Linking makes availability.ok true, which
  // unmounts the panel and stops the polling.
  useEffect(() => {
    if (!showOverride) return undefined;
    let cancelled = false;
    let baseline = null;
    const tick = async () => {
      const loras = await fetchMyUploads();
      if (cancelled || loras === null) return;
      if (baseline === null) {
        baseline = new Set(loras.map((m) => m.sd_name));
        return;
      }
      const fresh = loras.filter((m) => !baseline.has(m.sd_name));
      const ready = fresh.find((m) => m.ready);
      if (ready) {
        onPatch(entry.id, { sd_name_override: ready.sd_name });
      } else if (fresh.length > 0) {
        setDetected(fresh[0]);
      }
    };
    tick();
    const iv = setInterval(tick, 20000);
    return () => { cancelled = true; clearInterval(iv); setDetected(null); };
  }, [showOverride]); // eslint-disable-line react-hooks/exhaustive-deps

  const fam = baseFamily(entry.base_model);
  const availability = loraAvailability(entry, isLocal);
  const compatible = fam && checkpointFamily && fam === checkpointFamily;
  const dimmed = entry.active && !compatible;
  const pageUrl = loraLink(entry);
  const sourceName = entry.source === 'hf' ? 'Hugging Face' : 'Civitai';

  return (
    <div className={`bg-gray-950/60 border border-gray-800 rounded-lg p-3 space-y-2 ${!compatible ? 'opacity-60' : ''}`}>
      <div className="flex items-center gap-3">
        {entry.thumb_url ? (
          <a href={pageUrl} target="_blank" rel="noreferrer" className="shrink-0" title={`Open on ${sourceName}`}>
            <img src={entry.thumb_url} alt="" loading="lazy" className="w-10 h-10 rounded object-cover bg-gray-800" />
          </a>
        ) : (
          <div className="w-10 h-10 rounded bg-gray-800 shrink-0" />
        )}
        <div className="min-w-0 flex-1">
          <a
            href={pageUrl}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-gray-200 hover:text-purple-300 truncate block"
            title={`${entry.name} — ${entry.version_name}`}
          >
            {entry.name}
          </a>
          <div className="text-[10px] text-gray-500 truncate">
            {entry.creator && <span>by {entry.creator} · </span>}
            {entry.base_model}
            {entry.trained_words?.length > 0 && (
              <span className="text-gray-600"> · triggers: {entry.trained_words.slice(0, 3).join(', ')}</span>
            )}
          </div>
        </div>
        <span className={`px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider border shrink-0 ${availability.cls}`}>
          {availability.label}
        </span>
        <Toggle
          checked={!!entry.active}
          onChange={(v) => onPatch(entry.id, { active: v })}
          label=""
        />
        <button
          onClick={() => onDelete(entry.id)}
          className="text-gray-600 hover:text-red-400 transition-colors shrink-0"
          title="Remove from library"
        >
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
          </svg>
        </button>
      </div>

      {dimmed && (
        <p className="text-[11px] text-yellow-500">
          Active but the selected checkpoint is {FAMILY_LABELS[checkpointFamily] || 'unknown'} — this{' '}
          {FAMILY_LABELS[fam] || entry.base_model} LoRA will be skipped.
        </p>
      )}

      {entry.active && fam !== 'flux' && (
        <div className="flex items-center gap-3">
          <label
            className="text-[10px] uppercase tracking-wider text-gray-500 shrink-0"
            title={aiWeighted
              ? 'Default weight — before each image an AI picks the actual weight, following the instructions below'
              : `LoRA weight, ${LORA_WEIGHT_MIN} to ${LORA_WEIGHT_MAX} (0 disables, negative inverts the style)`}
          >
            {aiWeighted ? 'Default' : 'Weight'}
          </label>
          <input
            type="range" min={LORA_WEIGHT_MIN} max={LORA_WEIGHT_MAX} step={0.1}
            value={Number(strength) || 0}
            onChange={(e) => setStrength(Number(e.target.value))}
            onMouseUp={() => onPatch(entry.id, { strength: Number(strength) })}
            onTouchEnd={() => onPatch(entry.id, { strength: Number(strength) })}
            onKeyUp={() => onPatch(entry.id, { strength: Number(strength) })}
            className="w-full accent-purple-500"
          />
          <input
            type="number" min={LORA_WEIGHT_MIN} max={LORA_WEIGHT_MAX} step={0.05}
            value={strength}
            onChange={(e) => setStrength(e.target.value)}
            onBlur={commitStrength}
            onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur(); }}
            className="w-16 shrink-0 bg-gray-900 border border-gray-700 rounded px-1.5 py-1 text-xs text-gray-200 focus:border-purple-500 focus:outline-none"
          />
          <button
            type="button"
            onClick={cycleLlmMode}
            title={'Click to cycle what an AI decides for this LoRA before each image:\n'
              + 'off — always applied at the slider weight.\n'
              + 'gated — the condition below decides whether it applies.\n'
              + `weight — always applies; the AI picks the weight (${LORA_WEIGHT_MIN} to ${LORA_WEIGHT_MAX}), following the instructions below.\n`
              + 'both — the text below gates it AND guides the weight.'}
            className={`px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider border shrink-0 whitespace-nowrap transition-colors ${
              llmMode !== 'off'
                ? 'bg-purple-900/40 text-purple-300 border-purple-800'
                : 'bg-gray-900/40 text-gray-500 border-gray-800 hover:text-gray-300'
            }`}
          >
            {LORA_LLM_MODE_LABELS[llmMode]}
          </button>
        </div>
      )}

      {entry.active && (
        <div className="flex items-start gap-2">
          <label
            className={`text-[10px] uppercase tracking-wider shrink-0 pt-2.5 ${llmMode !== 'off' ? 'text-purple-400' : 'text-gray-500'}`}
            title={{
              off: 'Describe when this LoRA should be used; before each image an AI reads the scene and decides. Typing here switches the LoRA to gated.',
              gate: 'Before each image an AI reads the scene and applies this LoRA only when this condition holds.',
              weight: 'Instructions the AI follows to pick this LoRA’s weight per image — say what low vs high (or negative) weights mean.',
              both: 'One text, two jobs: it decides whether this LoRA applies AND how the AI should weight it.',
            }[llmMode]}
          >
            {{ off: 'Condition', gate: 'AI-gated', weight: 'Instructions', both: 'Gate + weight' }[llmMode]}
          </label>
          <AutoGrowTextarea
            value={condition}
            onChange={(e) => setCondition(e.target.value)}
            onBlur={commitCondition}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); e.target.blur(); }
            }}
            placeholder={{
              off: 'always on — describe a situation (e.g. battle scenes, at night) and an AI decides per image',
              gate: 'describe a situation (e.g. battle scenes, at night) — an AI decides per image',
              weight: 'what weights mean (e.g. 0.3 subtle by day, 1.5 in night battles) — an AI picks per image',
              both: 'when it applies and how to weight it (e.g. only in battles; 0.8 skirmish, 1.5 all-out war)',
            }[llmMode]}
            className={`${inputCls} text-xs`}
          />
        </div>
      )}

      {entry.active && (
        <div className="flex items-start gap-2">
          <label
            className="text-[10px] uppercase tracking-wider shrink-0 pt-2.5 text-gray-500"
            title="Trigger words woven into every prompt that uses this LoRA. Pulled from the model page — edit them here if they're wrong or missing. Comma-separated."
          >
            Triggers
          </label>
          <AutoGrowTextarea
            value={triggers}
            onChange={(e) => setTriggers(e.target.value)}
            onBlur={commitTriggers}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); e.target.blur(); }
            }}
            placeholder="none — comma-separated words injected into the prompt (e.g. glowing runes, ornate armor)"
            className={`${inputCls} text-xs`}
          />
        </div>
      )}

      {isLocal && download && <InstallProgress download={download} onCancel={onCancelInstall} />}

      {isLocal && !availability.ok && (!download || download.status === 'error') && (
        <div className="space-y-1.5">
          <div className="flex items-center gap-3 text-[11px]">
            {entry.download_url && !entry.gated && (
              <button
                onClick={() => onInstall(entry)}
                disabled={!canInstall}
                title={canInstall
                  ? `Download into your LoRA folder${entry.size_kb ? ` (${(entry.size_kb / 1024).toFixed(0)} MB)` : ''} and link it up`
                  : 'Set your LoRA folder (or the install helper) in Setup to enable one-click installs'}
                className="px-2 py-0.5 rounded bg-purple-600 hover:bg-purple-500 text-white disabled:opacity-40"
              >
                Install to WebUI
              </button>
            )}
            <button
              onClick={() => { setShowLink((s) => !s); if (!showLink) loadInstalledLoras(); }}
              className="text-gray-500 hover:text-gray-300"
            >
              {showLink ? 'Hide installed files' : 'Link to an installed file'}
            </button>
            <a
              href={`${API_BASE}/loras/${entry.id}/download`}
              className="text-gray-600 hover:text-gray-400 underline"
              title="Plain browser download — put the file in your LoRA folder yourself"
            >
              download file
            </a>
          </div>
          {!canInstall && entry.download_url && !entry.gated && (
            <p className="text-[10px] text-gray-600">
              Set your WebUI's LoRA folder in the Setup tab to enable one-click installs.
            </p>
          )}
          {showLink && (
            <div className="space-y-1 border border-gray-800 rounded-lg p-2.5 bg-gray-900/40">
              {installedLoras === null && <p className="text-[11px] text-gray-500 animate-pulse">Loading installed LoRAs…</p>}
              {installedLoras !== null && installedLoras.length === 0 && (
                <p className="text-[11px] text-gray-600 italic">
                  The WebUI reports no installed LoRAs — is it running with --api?
                </p>
              )}
              {installedLoras !== null && installedLoras.length > 5 && (
                <input
                  type="text"
                  value={linkFilter}
                  onChange={(e) => setLinkFilter(e.target.value)}
                  placeholder="Filter installed files…"
                  className={`${inputCls} text-xs`}
                />
              )}
              {(installedLoras || [])
                .filter((m) => !linkFilter || m.name.toLowerCase().includes(linkFilter.toLowerCase()))
                .slice(0, 30)
                .map((m) => (
                  <div key={m.name} className="flex items-center gap-2 text-[11px]">
                    <span className="text-gray-300 truncate flex-1" title={m.path || m.name}>{m.name}</span>
                    <button
                      onClick={() => { onPatch(entry.id, { local_name: m.name }); setShowLink(false); }}
                      className="px-2 py-0.5 rounded bg-purple-600 hover:bg-purple-500 text-white shrink-0"
                    >
                      Use
                    </button>
                  </div>
                ))}
            </div>
          )}
        </div>
      )}

      {!isLocal && !availability.ok && fam !== 'flux' && (
        <div className="space-y-1.5">
          <div className="flex items-center gap-3 text-[11px]">
            <button
              onClick={async () => { setBusy(true); await onRematch(entry.id); setBusy(false); }}
              disabled={busy}
              className="text-purple-400 hover:text-purple-300 disabled:opacity-40"
            >
              {busy ? 'Checking…' : 'Recheck Novita'}
            </button>
            <button
              onClick={() => { setShowOverride((s) => !s); loadMyUploads(); }}
              className="text-gray-500 hover:text-gray-300"
            >
              {showOverride ? 'Hide upload helper' : 'Upload it yourself'}
            </button>
          </div>
          {showOverride && (
            <div className="space-y-2 border border-gray-800 rounded-lg p-2.5 bg-gray-900/40">
              <div className="flex items-center justify-between">
                <p className="text-[11px] text-gray-400 font-medium">
                  Put this LoRA on your own Novita account
                </p>
                {myLoras !== null && (
                  <span className={`text-[10px] ${myLoras.length >= maxSlots ? 'text-red-400' : 'text-gray-600'}`}>
                    {myLoras.length} of {maxSlots} upload slots used
                  </span>
                )}
              </div>
              <ol className="space-y-1.5 text-[11px] text-gray-400 list-none">
                <li className="flex items-center gap-2">
                  <span className="text-gray-600 shrink-0">1.</span>
                  <a
                    href={`${API_BASE}/loras/${entry.id}/download`}
                    className="text-purple-400 hover:text-purple-300 underline"
                  >
                    Download the file
                    {entry.size_kb ? ` (${(entry.size_kb / 1024).toFixed(0)} MB)` : ''}
                  </a>
                  {entry.source !== 'hf' && <span className="text-gray-600">— uses your Civitai key</span>}
                </li>
                <li className="flex items-center gap-2">
                  <span className="text-gray-600 shrink-0">2.</span>
                  <a
                    href="https://novita.ai/models-console/model-management"
                    target="_blank" rel="noreferrer"
                    className="text-purple-400 hover:text-purple-300 underline"
                  >
                    Upload it in the Novita console
                  </a>
                  <span className="text-gray-600">— Upload Model, pick the file</span>
                </li>
                <li className="flex items-center gap-2">
                  <span className="text-gray-600 shrink-0">3.</span>
                  {detected ? (
                    <span className="text-yellow-500">
                      Found “{detected.name || detected.sd_name}” — Novita is processing it,
                      it will link itself when ready…
                    </span>
                  ) : (
                    <span className="text-gray-500">
                      Done — this entry links itself as soon as the upload appears
                      <span className="inline-block ml-1 animate-pulse">⏳</span>
                    </span>
                  )}
                </li>
              </ol>
              {myLoras !== null && myLoras.length > 0 && (
                <div className="space-y-1 pt-1 border-t border-gray-800">
                  <p className="text-[10px] uppercase tracking-wider text-gray-600">
                    …or link an existing upload
                  </p>
                  {myLoras.map((m) => (
                    <div key={m.sd_name} className="flex items-center gap-2 text-[11px]">
                      <span className="text-gray-300 truncate flex-1" title={m.sd_name}>
                        {m.name}
                        {m.base_model && <span className="text-gray-600"> · {m.base_model}</span>}
                        {!m.ready && <span className="text-yellow-600"> · processing</span>}
                      </span>
                      <button
                        onClick={() => onPatch(entry.id, { sd_name_override: m.sd_name })}
                        disabled={!m.ready}
                        className="px-2 py-0.5 rounded bg-purple-600 hover:bg-purple-500 text-white disabled:opacity-40 shrink-0"
                      >
                        Use
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <div className="flex items-start gap-2">
                <AutoGrowTextarea
                  value={override}
                  onChange={(e) => setOverride(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) e.preventDefault();
                  }}
                  placeholder="…or type the MODEL NAME IN API by hand"
                  className={`${inputCls} text-xs`}
                />
                <button
                  onClick={() => onPatch(entry.id, { sd_name_override: override })}
                  className="px-3 py-1 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-xs shrink-0"
                >
                  Set
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// The browser's last state (filters, fetched results, scroll offset), mirrored
// to localStorage so it reopens where it was — across tab switches, page
// reloads, and Android killing the backgrounded webview.
const LORA_BROWSER_KEY = 'wb_image_gen_lora_browser';
function loadLoraBrowserState() {
  try { return JSON.parse(localStorage.getItem(LORA_BROWSER_KEY)) || null; } catch (e) { return null; }
}

// LoRA browser (Civitai + Hugging Face) + local library. Browsing is proxied
// through the module backend (which injects the Civitai key for NSFW); saving
// stores metadata only — no file ever touches this device.
function LoraSection({ config, draft, set, library, setLibrary, checkpointFamily, onConfigRefresh }) {
  const [saved] = useState(loadLoraBrowserState);
  const isLocal = (draft.provider || config.provider) === 'local';
  // A writable folder on this machine, or the install helper next to a
  // remote WebUI — the backend routes the download either way.
  const canInstall = !!(config.local_install && config.local_install.lora);
  const [source, setSource] = useState(saved?.source || 'civitai');
  const [novitaOnly, setNovitaOnly] = useState(!!saved?.novitaOnly);
  const [query, setQuery] = useState(saved?.query || '');
  const [baseModel, setBaseModel] = useState(saved?.baseModel || '');
  const [loraType, setLoraType] = useState(saved?.loraType || 'LORA');
  const [category, setCategory] = useState(saved?.category || '');
  const [sort, setSort] = useState(saved?.sort || 'Most Downloaded');
  const [items, setItems] = useState(saved?.items || []);
  const [nextCursor, setNextCursor] = useState(saved?.nextCursor || '');
  const [loading, setLoading] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(!!saved?.open);
  const [recheckBusy, setRecheckBusy] = useState(false);
  const [myLoras, setMyLoras] = useState(null); // null = not fetched yet
  const [maxSlots, setMaxSlots] = useState(5);
  const debounceRef = useRef(null);
  const seqRef = useRef(0);
  const myLorasRef = useRef(false);
  const autoRecheckRef = useRef(false);
  // Restored results render as-is: the first auto-search is skipped so it
  // doesn't reset the list (and the scroll offset) back to page one.
  const skipSearchRef = useRef(!!(saved?.open && saved?.items?.length));
  const gridRef = useRef(null);
  const pendingScrollRef = useRef(saved?.scrollTop || 0);
  const scrollTopRef = useRef(saved?.scrollTop || 0);
  const scrollSaveRef = useRef(null);
  // Cursor already auto-fetched by scrolling, so a burst of scroll events
  // (loading isn't set until the next render) fires only one request per page.
  const autoLoadCursorRef = useRef('');

  // Hugging Face browsing is public, so the NSFW filter needs no key there;
  // Civitai requires its key for anything beyond SFW.
  const nsfwMode = (source === 'hf' || config.has_civitai_key) ? (draft.civitai_nsfw || 'off') : 'off';

  // "On Novita only" / "Installed only": keep results usable as-is — locally
  // that means hash-matched to an installed file; on Novita hash-matched in
  // the mirror, or flux (sent as a download link, no mirror needed).
  // Unknown availability (badge "?") is hidden too: it is not a known yes.
  const usableNow = (i) => !i.gated && (isLocal
    ? i.local_available === true
    : (i.novita_available === true ||
      (baseFamily(i.base_model) === 'flux' && i.download_url)));
  const visibleItems = novitaOnly ? items.filter(usableNow) : items;
  const savedIds = new Set(library.map((e) => e.id));
  const isUnmatched = (e) => (isLocal
    ? !(e.local && e.local.name)
    : baseFamily(e.base_model) !== 'flux' &&
      !(e.novita && e.novita.sd_name_in_api) &&
      !e.sd_name_override);
  const unmatchedCount = library.filter(isUnmatched).length;

  // In-flight and recent installs, polled while any download is running so
  // rows show live progress; a finishing install links its library entry
  // server-side, so completion refreshes the whole config.
  const [downloads, setDownloads] = useState([]);
  const [installedLoras, setInstalledLoras] = useState(null);
  const [versionPickerFor, setVersionPickerFor] = useState(null);   // browse item id
  const downloadsPollRef = useRef(null);
  const downloadingIdsRef = useRef(new Set());
  const installedLorasRef = useRef(false);

  const refreshDownloads = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/local/downloads`);
      if (!res.ok) return;
      const data = await res.json();
      const list = data.downloads || [];
      const nowDownloading = new Set(
        list.filter((d) => d.status === 'downloading').map((d) => d.id));
      const finished = [...downloadingIdsRef.current].some((id) => !nowDownloading.has(id));
      downloadingIdsRef.current = nowDownloading;
      setDownloads(list);
      if (finished) onConfigRefresh();   // pick up auto-linked entries
    } catch (e) { /* retried by the poller */ }
  }, [onConfigRefresh]);

  useEffect(() => {
    if (isLocal) refreshDownloads();
  }, [isLocal, refreshDownloads]);

  useEffect(() => {
    const active = downloads.some((d) => d.status === 'downloading');
    if (active && !downloadsPollRef.current) {
      downloadsPollRef.current = setInterval(refreshDownloads, 1000);
    } else if (!active && downloadsPollRef.current) {
      clearInterval(downloadsPollRef.current);
      downloadsPollRef.current = null;
    }
    return () => {
      if (downloadsPollRef.current) {
        clearInterval(downloadsPollRef.current);
        downloadsPollRef.current = null;
      }
    };
  }, [downloads, refreshDownloads]);

  const startInstall = useCallback(async (payload) => {
    setError('');
    try {
      const res = await fetch(`${API_BASE}/local/downloads`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      await refreshDownloads();
      return data.download;
    } catch (e) {
      setError(String(e.message || e));
      return null;
    }
  }, [refreshDownloads]);

  const cancelInstall = useCallback(async (dlId) => {
    await fetch(`${API_BASE}/local/downloads/${dlId}`, { method: 'DELETE' }).catch(() => {});
    refreshDownloads();
  }, [refreshDownloads]);

  // Latest download per library entry, for row progress bars.
  const downloadByLoraId = {};
  for (const d of downloads) {
    if (d.lora_id && !downloadByLoraId[d.lora_id]) downloadByLoraId[d.lora_id] = d;
  }

  const loadInstalledLoras = useCallback(async () => {
    if (installedLorasRef.current) return;
    installedLorasRef.current = true;
    try {
      const res = await fetch(`${API_BASE}/local/loras`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      setInstalledLoras(data.loras || []);
    } catch (e) {
      setInstalledLoras([]);
      setError(String(e.message || e));
    }
  }, []);

  const search = useCallback(async (cursor = '') => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError('');
    try {
      const params = new URLSearchParams({ query, sort, nsfw: nsfwMode });
      if (source === 'civitai') {
        params.set('lora_type', loraType);
        if (category) params.set('category', category);
      }
      if (baseModel) params.set('base_model', baseModel);
      if (cursor) params.set('cursor', cursor);
      const url = `${API_BASE}/${source === 'hf' ? 'hf' : 'civitai'}/loras?${params}`;
      let res = await fetch(url);
      // 503 = the source is temporarily overloaded, not a real failure: keep
      // the spinner up and retry every second (a newer search supersedes via
      // seq, and after a minute of no luck the error surfaces normally).
      for (let attempt = 0; res.status === 503 && attempt < 60; attempt++) {
        if (seq !== seqRef.current) return;
        setRetrying(true);
        await new Promise((resolve) => setTimeout(resolve, 1000));
        if (seq !== seqRef.current) return;
        res = await fetch(url);
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (seq !== seqRef.current) return;
      setItems((prev) => (cursor ? mergeBrowseResults(prev, data.items, sort) : data.items));
      setNextCursor(data.next_cursor || '');
    } catch (e) {
      if (seq === seqRef.current) setError(String(e.message || e));
    } finally {
      if (seq === seqRef.current) { setLoading(false); setRetrying(false); }
    }
  }, [source, query, baseModel, loraType, category, sort, nsfwMode]);

  // Sort options and base-model lists differ per source, so switching resets
  // the filters that don't carry over; the effect below re-searches.
  const switchSource = (s) => {
    if (s === source) return;
    setSource(s);
    setSort('Most Downloaded');
    setBaseModel('');
    setItems([]);
    setNextCursor('');
  };

  useEffect(() => {
    if (!open) return undefined;
    if (skipSearchRef.current) {
      skipSearchRef.current = false;
      return undefined;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(), 400);
    return () => clearTimeout(debounceRef.current);
  }, [open, search]);

  useEffect(() => {
    try {
      localStorage.setItem(LORA_BROWSER_KEY, JSON.stringify({
        open, source, query, baseModel, loraType, category, sort, novitaOnly,
        items, nextCursor, scrollTop: scrollTopRef.current,
      }));
    } catch (e) { /* storage unavailable or full */ }
  }, [open, source, query, baseModel, loraType, category, sort, novitaOnly, items, nextCursor]);

  // Put the results grid back where it was once the restored items render.
  // Thumbnails have fixed heights, so the offset is valid before they load.
  useEffect(() => {
    if (!open || !pendingScrollRef.current || !gridRef.current) return;
    gridRef.current.scrollTop = pendingScrollRef.current;
    pendingScrollRef.current = 0;
  }, [open, items.length]);

  const saveScrollTop = () => {
    try {
      const state = JSON.parse(localStorage.getItem(LORA_BROWSER_KEY)) || {};
      state.scrollTop = scrollTopRef.current;
      localStorage.setItem(LORA_BROWSER_KEY, JSON.stringify(state));
    } catch (e) { /* storage unavailable or full */ }
  };

  const onGridScroll = () => {
    const el = gridRef.current;
    scrollTopRef.current = el ? el.scrollTop : 0;
    if (scrollSaveRef.current) clearTimeout(scrollSaveRef.current);
    scrollSaveRef.current = setTimeout(saveScrollTop, 250);
    // Infinite scroll: fetch the next page as the bottom approaches. A page
    // that fails keeps its cursor in autoLoadCursorRef, so scrolling won't
    // hammer the API — the Load more button below stays as the manual retry.
    if (
      el && !loading && nextCursor && autoLoadCursorRef.current !== nextCursor &&
      el.scrollTop + el.clientHeight >= el.scrollHeight - 200
    ) {
      autoLoadCursorRef.current = nextCursor;
      search(nextCursor);
    }
  };

  // Flush a pending scroll save when the section unmounts (tab switch).
  useEffect(() => () => {
    if (scrollSaveRef.current) {
      clearTimeout(scrollSaveRef.current);
      saveScrollTop();
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const callLibrary = async (path, options) => {
    setError('');
    try {
      const res = await fetch(`${API_BASE}${path}`, options);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (data.lora_library) setLibrary(data.lora_library);
      return data;
    } catch (e) {
      setError(String(e.message || e));
      return null;
    }
  };

  // The account's own Novita console uploads, shared by every row's upload
  // helper. fetchMyUploads always hits the API (the helper polls it while
  // waiting for a new upload to appear); loadMyUploads is the once-per-visit
  // initial load.
  const fetchMyUploads = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/novita/my-loras`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setMyLoras(data.loras || []);
      if (data.max_slots) setMaxSlots(data.max_slots);
      return data.loras || [];
    } catch (e) {
      setMyLoras((prev) => prev ?? []);
      return null;
    }
  }, []);

  const loadMyUploads = useCallback(() => {
    if (myLorasRef.current) return;
    myLorasRef.current = true;
    fetchMyUploads();
  }, [fetchMyUploads]);

  const recheckAll = async () => {
    setRecheckBusy(true);
    try {
      await callLibrary(isLocal ? '/local/match-loras' : '/loras/match_all',
        { method: 'POST' });
    } finally {
      setRecheckBusy(false);
    }
  };

  // Novita's mirror grows over time — silently recheck unmatched entries
  // once per studio visit when the last check is a week old or older.
  // (Novita-only: the local equivalent is the explicit Match installed scan.)
  useEffect(() => {
    if (isLocal || autoRecheckRef.current || !config.has_key) return;
    const weekAgo = Date.now() - 7 * 24 * 3600 * 1000;
    const stale = library.some(
      (e) => isUnmatched(e) &&
        (!e.novita_checked_at || new Date(e.novita_checked_at).getTime() < weekAgo));
    if (!stale) return;
    autoRecheckRef.current = true;
    callLibrary('/loras/match_all', { method: 'POST' });
  }, [library, config.has_key]); // eslint-disable-line react-hooks/exhaustive-deps

  const saveLora = (item) =>
    callLibrary('/loras', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(item),
    });
  const patchLora = (id, patch) =>
    callLibrary(`/loras/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
  const deleteLora = (id) => callLibrary(`/loras/${id}`, { method: 'DELETE' });
  const rematchLora = (id) => callLibrary(`/loras/${id}/match`, { method: 'POST' });

  const activeCount = library.filter((e) => e.active && baseFamily(e.base_model) === checkpointFamily).length;

  return (
    <section className={sectionCls}>
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-300">
          LoRAs {library.length > 0 && <span className="text-gray-600">({library.length} saved{activeCount > 0 ? `, ${activeCount} active` : ''})</span>}
        </h2>
        <div className="flex items-center gap-3">
          {isLocal && canInstall && library.length > 0 && (
            <button
              onClick={recheckAll}
              disabled={recheckBusy}
              className="text-xs text-gray-500 hover:text-gray-300 disabled:opacity-40"
              title="Hash-scan your LoRA folder and link library entries to the files it finds"
            >
              {recheckBusy ? 'Scanning…' : unmatchedCount > 0 ? `Match installed (${unmatchedCount})` : 'Match installed'}
            </button>
          )}
          {!isLocal && unmatchedCount > 0 && config.has_key && (
            <button
              onClick={recheckAll}
              disabled={recheckBusy}
              className="text-xs text-gray-500 hover:text-gray-300 disabled:opacity-40"
              title="Re-search Novita's catalog for library LoRAs marked 'not on Novita'"
            >
              {recheckBusy ? 'Rechecking…' : `Recheck all (${unmatchedCount})`}
            </button>
          )}
          <button
            onClick={() => setOpen((o) => !o)}
            className="text-xs text-purple-400 hover:text-purple-300"
          >
            {open ? 'Close browser' : 'Browse LoRAs…'}
          </button>
        </div>
      </div>
      <p className="text-xs text-gray-600">
        {isLocal ? (
          <>Find LoRAs on Civitai or Hugging Face and press Install — the file lands in your WebUI's LoRA
          folder and links up automatically. Active LoRAs that do not match the selected checkpoint are
          skipped.</>
        ) : (
          <>Save LoRAs you like from Civitai or Hugging Face, then activate them. SD-family LoRAs are applied
          through Novita's mirrored catalog; Flux LoRAs are sent as download links (FLUX.2 model only). Active
          LoRAs that do not match the selected checkpoint are skipped.</>
        )}
      </p>

      {open && (
        <div className="space-y-3">
          <div className="flex gap-1">
            {[['civitai', 'Civitai'], ['hf', 'Hugging Face']].map(([id, label]) => (
              <button
                key={id}
                onClick={() => switchSource(id)}
                className={`px-3 py-1 rounded-full text-xs whitespace-nowrap transition-colors ${
                  source === id
                    ? 'bg-purple-600 text-white'
                    : 'bg-gray-900 border border-gray-800 text-gray-400 hover:text-gray-200'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search LoRAs…"
              className={inputCls}
            />
            <select value={baseModel} onChange={(e) => setBaseModel(e.target.value)} className={inputCls}>
              <option value="">All base models</option>
              {((source === 'hf' ? config.hf_base_models : config.civitai_base_models) || []).map(
                (b) => <option key={b} value={b}>{b}</option>)}
            </select>
            {source === 'civitai' && (
              <select value={loraType} onChange={(e) => setLoraType(e.target.value)} className={inputCls}>
                {(config.civitai_lora_types || ['LORA']).map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            )}
            {source === 'civitai' && (
              <select value={category} onChange={(e) => setCategory(e.target.value)} className={inputCls}>
                <option value="">All categories</option>
                {(config.civitai_categories || []).map((c) => (
                  <option key={c} value={c}>{c.charAt(0).toUpperCase() + c.slice(1)}</option>
                ))}
              </select>
            )}
            <select value={sort} onChange={(e) => setSort(e.target.value)} className={inputCls}>
              {((source === 'hf' ? config.hf_sorts : config.civitai_sorts) || []).map(
                (s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div className="flex items-center gap-4">
            <select
              value={nsfwMode}
              onChange={(e) => set('civitai_nsfw', e.target.value)}
              disabled={source === 'civitai' && !config.has_civitai_key}
              className={`${inputCls} max-w-[160px] disabled:opacity-50`}
            >
              <option value="off">No NSFW</option>
              <option value="include">NSFW</option>
              <option value="only">NSFW only</option>
            </select>
            <label
              className="flex items-center gap-1.5 text-[11px] text-gray-400 shrink-0 cursor-pointer"
              title={isLocal
                ? 'Only show LoRAs already installed in your LoRA folder. Hides ones with unknown availability.'
                : "Only show LoRAs that work right away: mirrored in Novita's catalog, or Flux LoRAs sent as download links. Hides ones with unknown availability."}
            >
              <input
                type="checkbox"
                checked={novitaOnly}
                onChange={(e) => setNovitaOnly(e.target.checked)}
                className="accent-purple-500"
              />
              {isLocal ? 'Installed only' : 'On Novita only'}
            </label>
            {source === 'civitai' && !config.has_civitai_key && (
              <span className="text-[11px] text-yellow-600">Save a Civitai API key (Setup tab) to browse NSFW LoRAs.</span>
            )}
          </div>

          {error && <p className="text-xs text-red-400">{error}</p>}

          <div
            ref={gridRef}
            onScroll={onGridScroll}
            className="grid grid-cols-2 sm:grid-cols-3 gap-3 max-h-96 overflow-y-auto pr-1"
          >
            {visibleItems.map((item) => {
              const pageUrl = loraLink(item);
              const badge = browseAvailability(item, isLocal);
              const itemDownload = downloads.find(
                (d) => d.lora_id === item.id && d.status === 'downloading');
              // A finished install marks the cached browse result installed
              // without waiting for a fresh search to re-annotate it.
              const itemInstalled = item.local_available === true ||
                downloads.some((d) => d.lora_id === item.id && d.status === 'done');
              return (
                <div key={item.id} className="bg-gray-950/60 border border-gray-800 rounded-lg overflow-hidden">
                  {item.thumb_url ? (
                    <a href={pageUrl} target="_blank" rel="noreferrer" title={`Open on ${item.source === 'hf' ? 'Hugging Face' : 'Civitai'}`}>
                      <img src={item.thumb_url} alt="" loading="lazy" className="w-full h-28 object-cover bg-gray-800" />
                    </a>
                  ) : (
                    <div className="w-full h-28 bg-gray-800" />
                  )}
                  <div className="p-2 space-y-1">
                    <a
                      href={pageUrl} target="_blank" rel="noreferrer"
                      className="text-xs text-gray-200 hover:text-purple-300 line-clamp-2 leading-snug"
                      title={item.name}
                    >
                      {item.name}
                    </a>
                    <div className="flex items-center justify-between text-[10px] text-gray-500">
                      <span className="truncate">{item.base_model}</span>
                      <span className="shrink-0">⬇ {fmtCount(item.stats?.downloads)} · 👍 {fmtCount(item.stats?.likes)}</span>
                    </div>
                    {badge && (
                      <div className="flex items-center gap-1">
                        <span
                          className={`px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wider border ${badge.cls}`}
                          title={badge.title}
                        >
                          {badge.label}
                        </span>
                        {item.file_count > 1 && (
                          <span className="text-[9px] text-gray-600" title="This repo holds several .safetensors files; the largest one is used">
                            {item.file_count} files
                          </span>
                        )}
                      </div>
                    )}
                    {isLocal && itemDownload ? (
                      <InstallProgress download={itemDownload} onCancel={cancelInstall} />
                    ) : isLocal && itemInstalled ? (
                      <p className="w-full px-2 py-1 rounded bg-green-900/40 border border-green-800 text-green-300 text-[11px] font-medium text-center">
                        Installed ✓
                      </p>
                    ) : isLocal && !item.gated && (item.download_url || item.model_id) ? (
                      <button
                        onClick={() => {
                          if (!canInstall) return;
                          if (item.source !== 'hf' && item.model_id) {
                            setVersionPickerFor(versionPickerFor === item.id ? null : item.id);
                          } else {
                            startInstall({ item });
                          }
                        }}
                        disabled={!canInstall}
                        title={canInstall
                          ? (item.source !== 'hf' && item.model_id
                            ? 'Pick a version, then it downloads into your LoRA folder and links up'
                            : 'Download into your LoRA folder and link it up')
                          : 'Set your LoRA folder (or the install helper) in Setup to enable one-click installs'}
                        className="w-full px-2 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white text-[11px] font-medium disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        Install
                      </button>
                    ) : (
                      <button
                        onClick={() => saveLora(item)}
                        disabled={savedIds.has(item.id) || item.gated}
                        title={item.gated
                          ? (isLocal ? 'Gated repos cannot be downloaded automatically' : 'Gated repos cannot be fetched by Novita')
                          : undefined}
                        className="w-full px-2 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white text-[11px] font-medium disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        {savedIds.has(item.id) ? 'Saved ✓' : item.gated ? 'Gated' : 'Save to library'}
                      </button>
                    )}
                    {isLocal && versionPickerFor === item.id && (
                      <VersionPicker
                        item={item}
                        onClose={() => setVersionPickerFor(null)}
                        onInstall={async (version) => {
                          const started = await startInstall({ item: version });
                          if (started) setVersionPickerFor(null);
                        }}
                      />
                    )}
                  </div>
                </div>
              );
            })}
          </div>
          {loading && (
            <p className="text-xs text-gray-500 animate-pulse">
              {retrying
                ? `${source === 'hf' ? 'Hugging Face' : 'Civitai'} search is overloaded — retrying…`
                : `Searching ${source === 'hf' ? 'Hugging Face' : 'Civitai'}…`}
            </p>
          )}
          {!loading && novitaOnly && items.length > visibleItems.length && (
            <p className="text-xs text-gray-600">
              {items.length - visibleItems.length} of {items.length} results hidden{' '}
              ({isLocal ? 'not installed' : 'not on Novita'})
              {visibleItems.length === 0 && nextCursor ? ' — try Load more' : ''}.
            </p>
          )}
          {!loading && items.length === 0 && !error && (
            <p className="text-xs text-gray-600 italic">No LoRAs found.</p>
          )}
          {!loading && nextCursor && (
            <button onClick={() => search(nextCursor)} className="text-xs text-purple-400 hover:text-purple-300">
              Load more…
            </button>
          )}
        </div>
      )}

      {!open && error && <p className="text-xs text-red-400">{error}</p>}

      {library.length === 0 ? (
        <p className="text-sm text-gray-600 italic">No saved LoRAs yet — browse Civitai or Hugging Face to add some.</p>
      ) : (
        <div className="space-y-2">
          {library.map((entry) => (
            <LoraRow
              key={entry.id}
              entry={entry}
              checkpointFamily={checkpointFamily}
              provider={isLocal ? 'local' : 'novita'}
              canInstall={canInstall}
              download={downloadByLoraId[entry.id]}
              onInstall={(e) => startInstall({ lora_id: e.id })}
              onCancelInstall={cancelInstall}
              installedLoras={installedLoras}
              loadInstalledLoras={loadInstalledLoras}
              onPatch={patchLora}
              onDelete={deleteLora}
              onRematch={rematchLora}
              myLoras={myLoras}
              maxSlots={maxSlots}
              loadMyUploads={loadMyUploads}
              fetchMyUploads={fetchMyUploads}
            />
          ))}
        </div>
      )}
    </section>
  );
}

const CKPT_BROWSER_KEY = 'wb_image_gen_ckpt_browser';

function loadCkptBrowserState() {
  try { return JSON.parse(localStorage.getItem(CKPT_BROWSER_KEY)) || null; } catch (e) { return null; }
}

function fmtSizeKB(kb) {
  kb = Number(kb) || 0;
  if (!kb) return '';
  if (kb >= 1024 * 1024) return `${(kb / (1024 * 1024)).toFixed(1)} GB`;
  return `${(kb / 1024).toFixed(0)} MB`;
}

function stripModelExt(name) {
  return String(name || '').replace(/\.(safetensors|ckpt|pt)$/i, '');
}

// Availability badge for a checkpoint browse result: hash-matched against a
// scan of the configured checkpoint folder (the checkpoint twin of
// browseAvailability's local branch).
function ckptAvailability(item) {
  if (item.local_available === true) {
    return {
      label: 'installed', cls: 'bg-green-900/50 text-green-300 border-green-800',
      title: item.local_name ? `In your checkpoint folder as ${item.local_name}` : 'In your checkpoint folder',
    };
  }
  if (item.local_available === false) {
    return {
      label: 'not installed', cls: 'bg-gray-800/80 text-gray-400 border-gray-700',
      title: 'Not in your checkpoint folder yet — press Install',
    };
  }
  return {
    label: 'installed: ?', cls: 'bg-gray-900/40 text-gray-600 border-gray-800',
    title: 'Local availability unknown — the WebUI did not answer. Check the connection in Setup '
      + '(installed models are matched through the WebUI API, or a scan of the checkpoint folder if set).',
  };
}

// Civitai checkpoint browser for the local provider (Setup tab). Mirrors the
// LoRA browser: search/filter/sort proxied through the module backend (which
// injects the Civitai key), infinite scroll with persisted state, one-click
// installs into the WebUI's checkpoint folder, and a "Use as model" shortcut
// that selects the installed file and carries Civitai's base-model metadata
// into the profile (better than the filename guess).
function CheckpointBrowser({ config, draft, set, setDraft }) {
  const [saved] = useState(loadCkptBrowserState);
  // A writable folder on this machine, or the install helper next to a
  // remote WebUI — the backend routes the download either way.
  const canInstall = !!(config.local_install && config.local_install.checkpoint);
  const [open, setOpen] = useState(!!saved?.open);
  const [query, setQuery] = useState(saved?.query || '');
  const [baseModel, setBaseModel] = useState(saved?.baseModel || '');
  const [category, setCategory] = useState(saved?.category || '');
  const [sort, setSort] = useState(saved?.sort || 'Most Downloaded');
  const [installedOnly, setInstalledOnly] = useState(!!saved?.installedOnly);
  const [items, setItems] = useState(saved?.items || []);
  const [nextCursor, setNextCursor] = useState(saved?.nextCursor || '');
  const [loading, setLoading] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');   // copy-link feedback
  const [versionPickerFor, setVersionPickerFor] = useState(null);   // browse item id
  const [downloads, setDownloads] = useState([]);
  const debounceRef = useRef(null);
  const seqRef = useRef(0);
  const skipSearchRef = useRef(!!(saved?.open && saved?.items?.length));
  const gridRef = useRef(null);
  const pendingScrollRef = useRef(saved?.scrollTop || 0);
  const scrollTopRef = useRef(saved?.scrollTop || 0);
  const scrollSaveRef = useRef(null);
  const autoLoadCursorRef = useRef('');
  const downloadsPollRef = useRef(null);

  const nsfwMode = config.has_civitai_key ? (draft.civitai_nsfw || 'off') : 'off';

  const refreshDownloads = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/local/downloads`);
      if (!res.ok) return;
      const data = await res.json();
      setDownloads((data.downloads || []).filter((d) => d.kind === 'checkpoint'));
    } catch (e) { /* retried by the poller */ }
  }, []);

  useEffect(() => {
    if (open) refreshDownloads();
  }, [open, refreshDownloads]);

  useEffect(() => {
    const active = downloads.some((d) => d.status === 'downloading');
    if (active && !downloadsPollRef.current) {
      downloadsPollRef.current = setInterval(refreshDownloads, 1000);
    } else if (!active && downloadsPollRef.current) {
      clearInterval(downloadsPollRef.current);
      downloadsPollRef.current = null;
    }
    return () => {
      if (downloadsPollRef.current) {
        clearInterval(downloadsPollRef.current);
        downloadsPollRef.current = null;
      }
    };
  }, [downloads, refreshDownloads]);

  const startInstall = useCallback(async (version) => {
    setError('');
    try {
      const res = await fetch(`${API_BASE}/local/downloads`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: 'checkpoint',
          url: version.download_url,
          sha256: version.sha256 || '',
          label: [version.name, version.version_name].filter(Boolean).join(' — '),
          filename: version.name || '',
          item_id: version.id,
          base_model: version.base_model || '',
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      await refreshDownloads();
      return data.download;
    } catch (e) {
      setError(String(e.message || e));
      return null;
    }
  }, [refreshDownloads]);

  const cancelInstall = useCallback(async (dlId) => {
    await fetch(`${API_BASE}/local/downloads/${dlId}`, { method: 'DELETE' }).catch(() => {});
    refreshDownloads();
  }, [refreshDownloads]);

  // Select an installed checkpoint as the profile's model: find the WebUI's
  // title for the file stem in the installed list (it may carry a subfolder
  // prefix and a short hash), preferring Civitai's base-model metadata over
  // the WebUI list's filename-inferred guess.
  const chooseModel = useCallback(async (stem, civitaiBase) => {
    setError('');
    try {
      const res = await fetch(`${API_BASE}/models`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      const low = String(stem || '').toLowerCase();
      const hit = (data.models || []).find((m) => low && m.sd_name.toLowerCase().includes(low));
      if (!hit) {
        throw new Error('The WebUI has not picked the file up yet — press "Rescan" in the Model dropdown, then try again');
      }
      setDraft((d) => ({ ...d, model_name: hit.sd_name, model_base: civitaiBase || hit.base_model || '' }));
    } catch (e) {
      setError(String(e.message || e));
    }
  }, [setDraft]);

  // Copy a version's download link, for WebUIs on another machine where this
  // app cannot write into the checkpoint folder. Civitai often requires auth
  // on the actual download, so the user may need `?token=<their key>` or a
  // logged-in browser on that machine.
  const copyLink = useCallback(async (version) => {
    const ok = await copyText(version.download_url);
    setNotice(ok
      ? `Link copied for "${[version.name, version.version_name].filter(Boolean).join(' — ')}" — download it into the WebUI machine's checkpoint folder (Civitai may need your API key: append ?token=…), then rescan.`
      : `Could not copy automatically — download link: ${version.download_url}`);
  }, []);

  const search = useCallback(async (cursor = '') => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError('');
    setNotice('');
    try {
      const params = new URLSearchParams({ query, sort, nsfw: nsfwMode });
      if (baseModel) params.set('base_model', baseModel);
      if (category) params.set('category', category);
      if (cursor) params.set('cursor', cursor);
      const url = `${API_BASE}/civitai/checkpoints?${params}`;
      let res = await fetch(url);
      // 503 = Civitai temporarily overloaded: keep the spinner up and retry
      // (a newer search supersedes via seq; the error surfaces after a minute).
      for (let attempt = 0; res.status === 503 && attempt < 60; attempt++) {
        if (seq !== seqRef.current) return;
        setRetrying(true);
        await new Promise((resolve) => setTimeout(resolve, 1000));
        if (seq !== seqRef.current) return;
        res = await fetch(url);
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (seq !== seqRef.current) return;
      setItems((prev) => (cursor ? mergeBrowseResults(prev, data.items, sort) : data.items));
      setNextCursor(data.next_cursor || '');
    } catch (e) {
      if (seq === seqRef.current) setError(String(e.message || e));
    } finally {
      if (seq === seqRef.current) { setLoading(false); setRetrying(false); }
    }
  }, [query, baseModel, category, sort, nsfwMode]);

  useEffect(() => {
    if (!open) return undefined;
    if (skipSearchRef.current) {
      skipSearchRef.current = false;
      return undefined;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(), 400);
    return () => clearTimeout(debounceRef.current);
  }, [open, search]);

  useEffect(() => {
    try {
      localStorage.setItem(CKPT_BROWSER_KEY, JSON.stringify({
        open, query, baseModel, category, sort, installedOnly,
        items, nextCursor, scrollTop: scrollTopRef.current,
      }));
    } catch (e) { /* storage unavailable or full */ }
  }, [open, query, baseModel, category, sort, installedOnly, items, nextCursor]);

  useEffect(() => {
    if (!open || !pendingScrollRef.current || !gridRef.current) return;
    gridRef.current.scrollTop = pendingScrollRef.current;
    pendingScrollRef.current = 0;
  }, [open, items.length]);

  const saveScrollTop = () => {
    try {
      const state = JSON.parse(localStorage.getItem(CKPT_BROWSER_KEY)) || {};
      state.scrollTop = scrollTopRef.current;
      localStorage.setItem(CKPT_BROWSER_KEY, JSON.stringify(state));
    } catch (e) { /* storage unavailable or full */ }
  };

  const onGridScroll = () => {
    const el = gridRef.current;
    scrollTopRef.current = el ? el.scrollTop : 0;
    if (scrollSaveRef.current) clearTimeout(scrollSaveRef.current);
    scrollSaveRef.current = setTimeout(saveScrollTop, 250);
    if (
      el && !loading && nextCursor && autoLoadCursorRef.current !== nextCursor &&
      el.scrollTop + el.clientHeight >= el.scrollHeight - 200
    ) {
      autoLoadCursorRef.current = nextCursor;
      search(nextCursor);
    }
  };

  useEffect(() => () => {
    if (scrollSaveRef.current) {
      clearTimeout(scrollSaveRef.current);
      saveScrollTop();
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const visibleItems = installedOnly ? items.filter((i) => i.local_available === true) : items;
  const stemInUse = (stem) =>
    !!stem && (draft.model_name || '').toLowerCase().includes(String(stem).toLowerCase());

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs uppercase tracking-wider text-gray-500">Model browser</span>
        <button
          onClick={() => setOpen((o) => !o)}
          className="text-xs text-purple-400 hover:text-purple-300"
        >
          {open ? 'Close browser' : 'Browse Civitai models…'}
        </button>
      </div>
      <p className="text-xs text-gray-600">
        {canInstall ? (
          <>Find checkpoints on Civitai and press Install — the file downloads into your WebUI's
          checkpoint folder. Once it finishes, press "Use as model" to select it here.</>
        ) : (
          <>Find checkpoints on Civitai. Installed ones are detected through the WebUI itself, so
          this works when the WebUI runs on another machine — press "Copy link" and download the
          file into that machine's checkpoint folder. (If the folder is on this machine or
          mounted here, set it above to enable one-click installs.)</>
        )}
      </p>

      {open && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search checkpoints…"
              className={inputCls}
            />
            <select value={baseModel} onChange={(e) => setBaseModel(e.target.value)} className={inputCls}>
              <option value="">All base models</option>
              {(config.civitai_base_models || []).map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
            <select value={category} onChange={(e) => setCategory(e.target.value)} className={inputCls}>
              <option value="">All categories</option>
              {(config.civitai_categories || []).map((c) => (
                <option key={c} value={c}>{c.charAt(0).toUpperCase() + c.slice(1)}</option>
              ))}
            </select>
            <select value={sort} onChange={(e) => setSort(e.target.value)} className={inputCls}>
              {(config.civitai_sorts || []).map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div className="flex items-center gap-4">
            <select
              value={nsfwMode}
              onChange={(e) => set('civitai_nsfw', e.target.value)}
              disabled={!config.has_civitai_key}
              className={`${inputCls} max-w-[160px] disabled:opacity-50`}
            >
              <option value="off">No NSFW</option>
              <option value="include">NSFW</option>
              <option value="only">NSFW only</option>
            </select>
            <label
              className="flex items-center gap-1.5 text-[11px] text-gray-400 shrink-0 cursor-pointer"
              title="Only show checkpoints already installed in your checkpoint folder. Hides ones with unknown availability."
            >
              <input
                type="checkbox"
                checked={installedOnly}
                onChange={(e) => setInstalledOnly(e.target.checked)}
                className="accent-purple-500"
              />
              Installed only
            </label>
            {!config.has_civitai_key && (
              <span className="text-[11px] text-yellow-600">Save a Civitai API key above to browse NSFW models.</span>
            )}
          </div>

          {error && <p className="text-xs text-red-400">{error}</p>}
          {notice && <p className="text-xs text-green-400 break-all">{notice}</p>}

          <div
            ref={gridRef}
            onScroll={onGridScroll}
            className="grid grid-cols-2 sm:grid-cols-3 gap-3 max-h-96 overflow-y-auto pr-1"
          >
            {visibleItems.map((item) => {
              const pageUrl = loraLink(item);
              const badge = ckptAvailability(item);
              const itemDownload = downloads.find(
                (d) => d.item_id === item.id && d.status === 'downloading');
              const doneDownload = downloads.find(
                (d) => d.item_id === item.id && d.status === 'done');
              // A finished install marks the cached browse result installed
              // without waiting for a fresh search to re-annotate it.
              const itemInstalled = item.local_available === true || !!doneDownload;
              const stem = item.local_name || stripModelExt(doneDownload?.filename);
              const inUse = stemInUse(stem);
              return (
                <div key={item.id} className="bg-gray-950/60 border border-gray-800 rounded-lg overflow-hidden">
                  {item.thumb_url ? (
                    <a href={pageUrl} target="_blank" rel="noreferrer" title="Open on Civitai">
                      <img src={item.thumb_url} alt="" loading="lazy" className="w-full h-28 object-cover bg-gray-800" />
                    </a>
                  ) : (
                    <div className="w-full h-28 bg-gray-800" />
                  )}
                  <div className="p-2 space-y-1">
                    <a
                      href={pageUrl} target="_blank" rel="noreferrer"
                      className="text-xs text-gray-200 hover:text-purple-300 line-clamp-2 leading-snug"
                      title={item.name}
                    >
                      {item.name}
                    </a>
                    <div className="flex items-center justify-between text-[10px] text-gray-500">
                      <span className="truncate">
                        {item.base_model}
                        {item.size_kb ? ` · ${fmtSizeKB(item.size_kb)}` : ''}
                      </span>
                      <span className="shrink-0">⬇ {fmtCount(item.stats?.downloads)} · 👍 {fmtCount(item.stats?.likes)}</span>
                    </div>
                    <span
                      className={`inline-block px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wider border ${badge.cls}`}
                      title={badge.title}
                    >
                      {badge.label}
                    </span>
                    {itemDownload ? (
                      <InstallProgress download={itemDownload} onCancel={cancelInstall} />
                    ) : itemInstalled ? (
                      <div className="flex gap-1">
                        <p className="flex-1 px-2 py-1 rounded bg-green-900/40 border border-green-800 text-green-300 text-[11px] font-medium text-center">
                          Installed ✓
                        </p>
                        {inUse ? (
                          <p className="px-2 py-1 rounded bg-purple-900/40 border border-purple-800 text-purple-300 text-[11px] font-medium shrink-0" title="Selected as the profile's model">
                            In use
                          </p>
                        ) : (
                          <button
                            onClick={() => chooseModel(stem, item.base_model)}
                            className="px-2 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white text-[11px] font-medium shrink-0"
                            title="Select this checkpoint as the profile's model"
                          >
                            Use
                          </button>
                        )}
                      </div>
                    ) : (
                      <button
                        onClick={() => setVersionPickerFor(versionPickerFor === item.id ? null : item.id)}
                        title={canInstall
                          ? 'Pick a version, then it downloads into your checkpoint folder'
                          : 'Pick a version and copy its download link — fetch the file on the machine running the WebUI'}
                        className="w-full px-2 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white text-[11px] font-medium"
                      >
                        {canInstall ? 'Install' : 'Copy link'}
                      </button>
                    )}
                    {versionPickerFor === item.id && (
                      <VersionPicker
                        item={item}
                        actionLabel={canInstall ? 'Install' : 'Copy link'}
                        onClose={() => setVersionPickerFor(null)}
                        onInstall={async (version) => {
                          if (canInstall) {
                            const started = await startInstall(version);
                            if (started) setVersionPickerFor(null);
                          } else {
                            await copyLink(version);
                            setVersionPickerFor(null);
                          }
                        }}
                      />
                    )}
                  </div>
                </div>
              );
            })}
          </div>
          {loading && (
            <p className="text-xs text-gray-500 animate-pulse">
              {retrying ? 'Civitai search is overloaded — retrying…' : 'Searching Civitai…'}
            </p>
          )}
          {!loading && installedOnly && items.length > visibleItems.length && (
            <p className="text-xs text-gray-600">
              {items.length - visibleItems.length} of {items.length} results hidden (not installed)
              {visibleItems.length === 0 && nextCursor ? ' — try Load more' : ''}.
            </p>
          )}
          {!loading && items.length === 0 && !error && (
            <p className="text-xs text-gray-600 italic">No checkpoints found.</p>
          )}
          {!loading && nextCursor && (
            <button onClick={() => search(nextCursor)} className="text-xs text-purple-400 hover:text-purple-300">
              Load more…
            </button>
          )}
        </div>
      )}

      {!open && error && <p className="text-xs text-red-400">{error}</p>}

      {downloads.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-[10px] uppercase tracking-wider text-gray-500">Checkpoint installs</p>
          {downloads.slice(0, 5).map((d) => (
            <div key={d.id} className="flex items-center gap-2">
              <div className="flex-1 min-w-0">
                <InstallProgress download={d} onCancel={cancelInstall} />
              </div>
              {d.status === 'done' && (
                stemInUse(stripModelExt(d.filename)) ? (
                  <span className="text-[11px] text-purple-300 shrink-0">In use</span>
                ) : (
                  <button
                    onClick={() => chooseModel(stripModelExt(d.filename), d.base_model)}
                    className="text-[11px] text-purple-400 hover:text-purple-300 shrink-0"
                  >
                    Use as model
                  </button>
                )
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ImageStudio({ onBack }) {
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState({});
  const [library, setLibrary] = useState([]);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [savedFlash, setSavedFlash] = useState(false);

  const [records, setRecords] = useState([]);
  const [pendingCount, setPendingCount] = useState(0);
  const [lightbox, setLightbox] = useState(null);
  // Filenames the user has revealed this visit, so a concealed image stays
  // open once clicked without uncovering its batch siblings (mirrors the
  // in-chat widget's per-session reveal behavior).
  const [revealed, setRevealed] = useState(() => new Set());

  // Persisted on every keystroke so the draft survives page reloads and
  // Android killing the backgrounded webview (there is no reliable
  // "about to shut down" event to save on).
  const [testPrompt, setTestPrompt] = useState(() => {
    try { return localStorage.getItem('wb_image_gen_test_prompt') || ''; } catch (e) { return ''; }
  });
  const updateTestPrompt = (value) => {
    setTestPrompt(value);
    try { localStorage.setItem('wb_image_gen_test_prompt', value); } catch (e) { /* ignore */ }
  };
  const [testRefine, setTestRefine] = useState(() => {
    try { return localStorage.getItem('wb_image_gen_test_refine') !== 'false'; } catch (e) { return true; }
  });
  const updateTestRefine = (value) => {
    setTestRefine(value);
    try { localStorage.setItem('wb_image_gen_test_refine', String(value)); } catch (e) { /* ignore */ }
  };
  const [testError, setTestError] = useState('');
  const [testBusy, setTestBusy] = useState(false);
  const pollRef = useRef(null);

  const [showKeyGuide, setShowKeyGuide] = useState(false);
  const [tab, setTab] = useState(() => {
    try {
      const saved = localStorage.getItem('wb_image_gen_tab');
      if (TABS.some((t) => t.id === saved)) return saved;
    } catch (e) { /* storage unavailable */ }
    return 'setup';
  });
  const switchTab = (id) => {
    setTab(id);
    try { localStorage.setItem('wb_image_gen_tab', id); } catch (e) { /* ignore */ }
  };

  const applyConfig = useCallback((data) => {
    setConfig(data);
    setDraft(data);
    setLibrary(data.lora_library || []);
  }, []);

  const loadConfig = useCallback(async () => {
    const res = await fetch(`${API_BASE}/config`);
    applyConfig(await res.json());
  }, [applyConfig]);

  // Refresh config + library without touching the draft, so background
  // changes (a finished install linking its entry) don't clobber unsaved
  // edits.
  const refreshLibrary = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/config`);
      if (!res.ok) return;
      const data = await res.json();
      setConfig(data);
      setLibrary(data.lora_library || []);
    } catch (e) { /* next poll retries */ }
  }, []);

  const loadImages = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/images?limit=200`);
      if (!res.ok) return;
      const data = await res.json();
      setRecords(data.records || []);
      setPendingCount(data.pending || 0);
    } catch (e) { /* retried by the poller */ }
  }, []);

  useEffect(() => {
    loadConfig().catch(() => setSaveError('Could not load config — is the server running?'));
    loadImages();
  }, [loadConfig, loadImages]);

  // Poll the gallery only while something is generating.
  useEffect(() => {
    if (pendingCount > 0 && !pollRef.current) {
      pollRef.current = setInterval(loadImages, 3000);
    } else if (pendingCount === 0 && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [pendingCount, loadImages]);

  // Keys are submitted (and validated) through their own KeyField buttons, so
  // they never count towards the draft's dirtiness.
  const dirty =
    config &&
    JSON.stringify({ ...draft, api_key: '', civitai_api_key: '', lora_library: [] }) !==
      JSON.stringify({ ...config, api_key: '', civitai_api_key: '', lora_library: [] });

  // Mirror of the backend's _prompt_style resolution, live against the draft:
  // an explicit prompt_style_mode wins, "auto" falls back to the
  // BOORU_TAG_MODEL_MARKERS heuristic.
  const modelIdent = `${draft.model_base || ''} ${draft.model_name || ''}`.toLowerCase();
  const autoPromptStyle = ['pony', 'illustrious', 'noob', 'animagine'].some((m) => modelIdent.includes(m))
    ? 'tags' : 'natural';
  const promptStyleMode = draft.prompt_style_mode || 'auto';
  const promptStyle = promptStyleMode === 'auto' ? autoPromptStyle : promptStyleMode;
  // Mirror of the backend's _tag_model_marker: the tag-trained family the
  // checkpoint matches, keying quality_tag_defaults. Null for natural models.
  const qualityMarker =
    ['pony', 'illustrious', 'noob', 'animagine'].find((m) => modelIdent.includes(m)) || null;
  const qualityDefault =
    (config?.quality_tag_defaults || {})[qualityMarker || 'pony'] || 'score_9, score_8_up, score_7_up';
  const isLocal = (draft.provider || config?.provider) === 'local';
  const checkpointFamily =
    draft.model_name === config?.flux2_model_name ? 'flux' : baseFamily(modelIdent);
  const activeLoraCount = library.filter(
    (e) => e.active && baseFamily(e.base_model) === checkpointFamily).length;

  const set = (key, value) => setDraft((d) => ({ ...d, [key]: value }));

  // The local WebUI reports its own sampler list; fall back to the static
  // Novita list until (or unless) it answers.
  const [localSamplers, setLocalSamplers] = useState(null);
  useEffect(() => {
    if (!isLocal) return undefined;
    let cancelled = false;
    fetch(`${API_BASE}/local/samplers`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => { if (!cancelled && data) setLocalSamplers(data.samplers || null); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [isLocal]);
  const samplerOptions = (isLocal && localSamplers) || config?.samplers || [];

  const save = async () => {
    setSaving(true);
    setSaveError('');
    try {
      const payload = {
        enabled: draft.enabled,
        model_name: draft.model_name,
        model_base: draft.model_base ?? '',
        width: Number(draft.width) || 1024,
        height: Number(draft.height) || 1024,
        image_num: Math.max(1, Number(draft.image_num) || 1),
        steps: Number(draft.steps) || 28,
        guidance_scale: Number(draft.guidance_scale) || 7,
        sampler_name: draft.sampler_name,
        negative_prompt: draft.negative_prompt,
        interval: Number(draft.interval) || 3,
        step_retries: Math.max(0, Number(draft.step_retries) || 0),
        prompt_model_preference: draft.prompt_model_preference,
        beat_planner: draft.beat_planner || 'fast',
        prompt_template: draft.prompt_template,
        prompt_template_tags: draft.prompt_template_tags,
        quality_tags: draft.quality_tags,
        booru_subject_mode: draft.booru_subject_mode || 'auto',
        booru_break_separator: draft.booru_break_separator === true,
        prompt_style_mode: draft.prompt_style_mode || 'auto',
        tag_usage_filter: draft.tag_usage_filter || 'off',
        tag_usage_min_count: Number(draft.tag_usage_min_count) || 0,
        style_suffix: draft.style_suffix,
        character_reference_enabled: draft.character_reference_enabled !== false,
        player_in_images: draft.player_in_images || 'show',
        chat_image_conceal: draft.chat_image_conceal || 'off',
        civitai_nsfw: draft.civitai_nsfw || 'off',
        provider: draft.provider || 'novita',
        local_base_url: draft.local_base_url ?? '',
        local_auth_user: draft.local_auth_user ?? '',
        // Masked round-trips are ignored server-side, so sending the field
        // unconditionally never clobbers a stored password.
        local_auth_pass: draft.local_auth_pass ?? '',
        local_checkpoint_dir: draft.local_checkpoint_dir ?? '',
        local_lora_dir: draft.local_lora_dir ?? '',
        local_helper_url: draft.local_helper_url ?? '',
        local_helper_token: draft.local_helper_token ?? '',
      };
      const res = await fetch(`${API_BASE}/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      applyConfig(await res.json());
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 2000);
    } catch (e) {
      setSaveError(String(e.message || e));
    } finally {
      setSaving(false);
    }
  };

  const testGenerate = async () => {
    setTestError('');
    setTestBusy(true);
    try {
      const res = await fetch(`${API_BASE}/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt_override: testPrompt.trim(), save_id: '__studio__', refine: testRefine }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      setPendingCount((c) => c + 1);
      loadImages();
    } catch (e) {
      setTestError(String(e.message || e));
      setTestBusy(false);
    }
  };

  // The button stays in its "generating" state from the click until the studio
  // image finishes. testBusy covers the brief window between the request and the
  // queued record appearing in the gallery; after that the record's own status
  // (below) drives it, so drop the local flag once the record shows up.
  const studioGenerating = records.some(
    (r) => r.save_id === '__studio__' &&
      (r.status === 'pending' || r.status === 'prompting' || r.status === 'generating'));
  useEffect(() => {
    if (studioGenerating) setTestBusy(false);
  }, [studioGenerating]);
  const generating = testBusy || studioGenerating;

  const deleteRecord = async (recordId) => {
    await fetch(`${API_BASE}/images/${recordId}`, { method: 'DELETE' }).catch(() => {});
    loadImages();
  };

  // How finished images appear in the library until clicked — same setting that
  // conceals story illustrations in chat (Output tab).
  const conceal = draft.chat_image_conceal || 'off';
  // Flat list of every finished image in the library; the fullscreen viewer
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
  const revealImage = (filename) =>
    setRevealed((prev) => {
      const next = new Set(prev);
      next.add(filename);
      return next;
    });
  const hideImage = (filename) =>
    setRevealed((prev) => {
      const next = new Set(prev);
      next.delete(filename);
      return next;
    });

  if (!config) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex items-center justify-center">
        {saveError ? (
          <p className="text-red-400">{saveError}</p>
        ) : (
          <div className="h-8 w-8 rounded-full border-2 border-gray-700 border-t-purple-400 animate-spin" />
        )}
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 text-gray-100">
      <div className="sticky top-0 z-20 bg-gray-950/90 backdrop-blur border-b border-gray-800">
        <div className="max-w-3xl mx-auto px-6 py-3 flex items-center justify-between">
          <button
            onClick={onBack}
            className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            Back to Menu
          </button>
          <button
            onClick={save}
            disabled={!dirty || saving}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              savedFlash
                ? 'bg-green-700 text-white'
                : 'bg-purple-600 hover:bg-purple-500 text-white disabled:opacity-40 disabled:cursor-not-allowed'
            }`}
          >
            {savedFlash ? 'Saved ✓' : saving ? 'Saving…' : dirty ? 'Save Changes' : 'Saved'}
          </button>
        </div>
      </div>
      <div className="max-w-3xl mx-auto px-6 py-6 space-y-6">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-3">🎨 Image Studio</h1>
          <p className="text-gray-500 mt-1 text-sm">
            Story illustrations via {isLocal ? 'your local Stable Diffusion WebUI' : 'the Novita AI API'}.
            Auto-generates every N turns; use /image in-game for on-demand shots.
          </p>
        </div>

        <ProfileBar config={config} dirty={dirty} onApply={applyConfig} onError={setSaveError} />

        <div className="flex gap-1 overflow-x-auto pb-1">
          {TABS.map((t) => {
            const badge =
              t.id === 'loras' && activeLoraCount > 0 ? activeLoraCount
                : t.id === 'library' && pendingCount > 0 ? pendingCount : null;
            return (
              <button
                key={t.id}
                onClick={() => switchTab(t.id)}
                className={`px-3 py-1.5 rounded-full text-sm whitespace-nowrap transition-colors shrink-0 ${
                  tab === t.id
                    ? 'bg-purple-600 text-white'
                    : 'bg-gray-900 border border-gray-800 text-gray-400 hover:text-gray-200'
                }`}
              >
                {t.label}
                {badge != null && (
                  <span
                    className={`ml-1.5 px-1.5 py-0.5 rounded-full text-[10px] ${
                      t.id === 'library'
                        ? 'bg-purple-900/60 text-purple-200 animate-pulse'
                        : 'bg-gray-800 text-gray-300'
                    }`}
                  >
                    {badge}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {saveError && (
          <div className="bg-red-950/50 border border-red-900 rounded-lg px-4 py-2 text-sm text-red-200">
            {saveError}
          </div>
        )}

        {/* Setup: provider + keys/connection + model */}
        {tab === 'setup' && (
        <section className={sectionCls}>
          <div>
            <label className={labelCls}>Image provider</label>
            <div className="flex gap-1">
              {[['novita', 'Novita AI (cloud)'], ['local', 'Local Stable Diffusion']].map(([id, label]) => (
                <button
                  key={id}
                  onClick={() => set('provider', id)}
                  className={`px-3 py-1.5 rounded-full text-sm whitespace-nowrap transition-colors ${
                    (draft.provider || 'novita') === id
                      ? 'bg-purple-600 text-white'
                      : 'bg-gray-900 border border-gray-800 text-gray-400 hover:text-gray-200'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
            <p className="text-xs text-gray-600 mt-1">
              {isLocal
                ? 'Runs on your own machine through an A1111/Forge-compatible WebUI — free, private, no content filter.'
                : 'Cloud rendering on novita.ai — no GPU needed, thousands of hosted checkpoints.'}
              {' '}Model and generation settings live in profiles, so keep one profile per provider and switch freely.
            </p>
          </div>

          {isLocal && <LocalConnectionCard config={config} draft={draft} set={set} />}

          {!isLocal && (
          <KeyField
            provider="novita"
            label="Novita API Key"
            placeholder="Paste your novita.ai key"
            saved={!!config.has_key}
            savedMask={config.api_key}
            onSaved={(data) => { setConfig(data); setLibrary(data.lora_library || []); }}
          >
            <p className="text-xs text-gray-600 mt-1">
              Checked against Novita on submit; stored locally on this machine only.{' '}
              <button
                onClick={() => setShowKeyGuide((s) => !s)}
                className="text-purple-400 hover:underline"
              >
                {showKeyGuide ? 'Hide quick start' : 'How do I get a key?'}
              </button>
            </p>
            {showKeyGuide && (
              <ol className="mt-2 space-y-1.5 text-xs text-gray-400 list-decimal list-inside bg-gray-950/60 border border-gray-800 rounded-lg p-3">
                <li>
                  Create an account at{' '}
                  <a href="https://novita.ai" target="_blank" rel="noreferrer" className="text-purple-400 hover:underline">
                    novita.ai
                  </a>{' '}
                  — Google or GitHub sign-in works, and new accounts get a small free credit.
                </li>
                <li>
                  Open{' '}
                  <a href="https://novita.ai/settings/key-management" target="_blank" rel="noreferrer" className="text-purple-400 hover:underline">
                    Key Management
                  </a>{' '}
                  (avatar menu → Account → API Keys).
                </li>
                <li>
                  Click <span className="text-gray-300 font-medium">+ Create New Key</span>, name it anything
                  (e.g. “worldbox”), and copy the key.
                </li>
                <li>
                  Paste it above and press <span className="text-gray-300 font-medium">Save Changes</span> — it
                  never leaves this machine.
                </li>
                <li>
                  Pick a model below and you're set. Standard images cost well under a cent each; top up under{' '}
                  <a href="https://novita.ai/billing" target="_blank" rel="noreferrer" className="text-purple-400 hover:underline">
                    Billing
                  </a>{' '}
                  once the free credit runs out.
                </li>
              </ol>
            )}
          </KeyField>
          )}
          <KeyField
            provider="civitai"
            label="Civitai API Key (optional)"
            placeholder="Paste your civitai.com key"
            saved={!!config.has_civitai_key}
            savedMask={config.civitai_api_key}
            onSaved={(data) => { setConfig(data); setLibrary(data.lora_library || []); }}
          >
            <p className="text-xs text-gray-600 mt-1">
              {isLocal
                ? 'Needed for NSFW LoRA browsing and for most Civitai downloads — one-click installs use it server-side.'
                : 'Needed for NSFW LoRA browsing and for FLUX.2 LoRA download links (Civitai requires auth on downloads).'}
              {' '}Create one under{' '}
              <a href="https://civitai.com/user/account" target="_blank" rel="noreferrer" className="text-purple-400 hover:underline">
                civitai.com account settings
              </a>
              . Stored locally on this machine only.
            </p>
          </KeyField>
          <KeyField
            provider="hf"
            label="Hugging Face Token (optional)"
            placeholder="Paste your huggingface.co token"
            saved={!!config.has_hf_key}
            savedMask={config.hf_api_key}
            onSaved={(data) => { setConfig(data); setLibrary(data.lora_library || []); }}
          >
            <p className="text-xs text-gray-600 mt-1">
              Optional — raises rate limits when browsing Hugging Face LoRAs; not needed for normal use.
              Create one under{' '}
              <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer" className="text-purple-400 hover:underline">
                huggingface.co token settings
              </a>
              . Stored locally on this machine only.
            </p>
          </KeyField>
          <ModelPicker
            value={draft.model_name}
            valueBase={draft.model_base}
            hasKey={!!config.has_key}
            local={isLocal}
            onSelect={(m) => setDraft((d) => ({ ...d, model_name: m.sd_name, model_base: m.base_model || '' }))}
          />
          {isLocal && draft.model_name && (
            <div>
              <label className={labelCls}>Base model</label>
              <select
                value={draft.model_base || ''}
                onChange={(e) => set('model_base', e.target.value)}
                className={inputCls}
              >
                <option value="">Unknown</option>
                {draft.model_base && !(config.civitai_base_models || []).includes(draft.model_base) && (
                  <option value={draft.model_base}>{draft.model_base}</option>
                )}
                {(config.civitai_base_models || []).map((b) => (
                  <option key={b} value={b}>{b}</option>
                ))}
              </select>
              <p className="text-xs text-gray-600 mt-1">
                Local checkpoints carry no metadata, so this is guessed from the filename. Correct it if
                wrong — it drives the automatic prompt style (booru tags vs natural language) and which
                LoRAs are considered compatible.
              </p>
            </div>
          )}
          {isLocal && (
            <CheckpointBrowser config={config} draft={draft} set={set} setDraft={setDraft} />
          )}
        </section>
        )}

        {/* Output */}
        {tab === 'output' && (
        <section className={sectionCls}>
          <Toggle
            checked={!!draft.enabled}
            onChange={(v) => set('enabled', v)}
            label="Auto-illustrate the story"
          />
          <div>
            <label className={labelCls}>Hide images in chat until clicked</label>
            <select
              value={draft.chat_image_conceal || 'off'}
              onChange={(e) => set('chat_image_conceal', e.target.value)}
              className={inputCls}
            >
              <option value="off">Off — show images immediately</option>
              <option value="blur">Blur until clicked</option>
              <option value="blackout">Black out until clicked</option>
            </select>
            <p className="text-xs text-gray-600 mt-1">
              New illustrations in the story arrive covered; click one to reveal it,
              and press its eye button to hide it again.
              Useful for surprise-sensitive scenes or reading in public.
            </p>
          </div>
          <div>
            <label className={labelCls}>Generate every N storyteller turns: {draft.interval}</label>
            <input
              type="range"
              min={1}
              max={20}
              value={draft.interval || 3}
              onChange={(e) => set('interval', Number(e.target.value))}
              className="w-full accent-purple-500"
            />
          </div>
          <div>
            <label className={labelCls}>
              Images per generation: {draft.image_num || 1}
            </label>
            <input
              type="range"
              min={1}
              max={config?.image_num_max ?? 4}
              value={draft.image_num || 1}
              onChange={(e) => set('image_num', Number(e.target.value))}
              className="w-full accent-purple-500"
            />
            <p className="text-xs text-gray-600 mt-1">
              Each image gets its own AI-written prompt covering its own
              consecutive beat of the scene, so the batch reads as a
              chronological sequence of what happened
              {isLocal
                ? ' (the local WebUI renders them one after another)'
                : ', all run in parallel on Novita (cost scales with the count)'}.
              Swipe or use the arrow keys in the fullscreen viewer to flip
              through them.
            </p>
          </div>
          {(Number(draft.image_num) || 1) > 1 && (
            <div>
              <label className={labelCls}>Beat planner (multi-image batches)</label>
              <select
                value={draft.beat_planner || 'fast'}
                onChange={(e) => set('beat_planner', e.target.value)}
                className={inputCls}
              >
                <option value="fast">Fast model slot (default)</option>
                <option value="smart">Prompt-writer model slot</option>
                <option value="off">Off</option>
              </select>
              <p className="text-xs text-gray-600 mt-1">
                One extra LLM call that splits the scene into as many beats as
                there are images before the prompt writers run, so every image
                agrees on the chronology. Fast keeps the added latency low;
                Off lets each writer split the scene on its own (images may
                disagree on the order of events).
              </p>
            </div>
          )}
          <div>
            <label className={labelCls}>
              Retries per failed step: {draft.step_retries ?? 1}
            </label>
            <input
              type="range"
              min={0}
              max={config?.step_retries_max ?? 5}
              value={draft.step_retries ?? 1}
              onChange={(e) => set('step_retries', Number(e.target.value))}
              className="w-full accent-purple-500"
            />
            <p className="text-xs text-gray-600 mt-1">
              How many extra times each generation step (prompt writing, image
              rendering) is re-run after a transient failure before the image is
              marked as failed. Permanent errors (bad API key, content-policy
              refusals) are never retried.
            </p>
          </div>
          <div>
            <label className={labelCls}>Image Size (pixels)</label>
            <div className="flex gap-3 items-center">
              <input
                type="number" min={128} max={2048} step={8}
                value={draft.width || 1024}
                onChange={(e) => set('width', e.target.value)}
                className={inputCls}
              />
              <span className="text-gray-600">×</span>
              <input
                type="number" min={128} max={2048} step={8}
                value={draft.height || 1024}
                onChange={(e) => set('height', e.target.value)}
                className={inputCls}
              />
            </div>
          </div>
          <div className="grid sm:grid-cols-2 gap-4">
            <div>
              <label className={labelCls}>Steps: {draft.steps}</label>
              <input
                type="range" min={1} max={100}
                value={draft.steps || 28}
                onChange={(e) => set('steps', Number(e.target.value))}
                className="w-full accent-purple-500"
              />
            </div>
            <div>
              <label className={labelCls}>Guidance Scale: {draft.guidance_scale}</label>
              <input
                type="range" min={1} max={30} step={0.5}
                value={draft.guidance_scale || 7}
                onChange={(e) => set('guidance_scale', Number(e.target.value))}
                className="w-full accent-purple-500"
              />
            </div>
          </div>
          <div>
            <label className={labelCls}>Sampler</label>
            <select
              value={draft.sampler_name || 'DPM++ 2M Karras'}
              onChange={(e) => set('sampler_name', e.target.value)}
              className={inputCls}
            >
              {draft.sampler_name && !samplerOptions.includes(draft.sampler_name) && (
                <option value={draft.sampler_name}>{draft.sampler_name}</option>
              )}
              {samplerOptions.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
        </section>
        )}

        {/* Prompting */}
        {tab === 'prompting' && (
        <section className={sectionCls}>
          <div>
            <label className={labelCls}>Prompt style</label>
            <select
              value={promptStyleMode}
              onChange={(e) => set('prompt_style_mode', e.target.value)}
              className={inputCls}
            >
              <option value="auto">
                Auto — detect from the model{draft.model_name ? ` (currently ${autoPromptStyle === 'tags' ? 'booru tags' : 'descriptive text'})` : ''}
              </option>
              <option value="tags">Booru tags</option>
              <option value="natural">Descriptive text</option>
            </select>
            <p className="text-xs text-gray-600 mt-1">
              How prompts are phrased: comma-separated booru tags (tag-trained models) or
              descriptive sentences. Auto detects from the base model — Pony/Illustrious/NoobAI/Animagine → tags,
              Flux and everything else → descriptive text.
            </p>
          </div>
          {draft.model_name && (
            <div className="text-xs text-gray-500 bg-gray-950/60 border border-gray-800 rounded-lg px-3 py-2">
              Selected model uses the{' '}
              <span className="text-purple-300 font-medium">
                {promptStyle === 'tags' ? 'booru tag' : 'natural language'}
              </span>{' '}
              template{qualityMarker ? ', with its family’s quality tags prepended' : ''}.
              {promptStyleMode === 'auto'
                ? ` Detected from base model${draft.model_base ? ` "${draft.model_base}"` : ' / model name'}.`
                : ' Set by the prompt style choice above.'}
            </div>
          )}
          <div>
            <label className={labelCls}>Prompt-writer model slot</label>
            <select
              value={draft.prompt_model_preference || 'smartest'}
              onChange={(e) => set('prompt_model_preference', e.target.value)}
              className={inputCls}
            >
              <option value="smartest">Smartest (default)</option>
              <option value="balanced">Balanced</option>
              <option value="fastest">Fastest</option>
            </select>
            <p className="text-xs text-gray-600 mt-1">
              The LLM that turns the latest scene into an image prompt before the image
              provider is called.
            </p>
          </div>
          <div className="space-y-2">
            <Toggle
              checked={draft.character_reference_enabled !== false}
              onChange={(v) => set('character_reference_enabled', v)}
              label="Keep known character appearances consistent"
            />
            <p className="text-xs text-gray-600">
              Feeds canonical appearances from the Player Character Tracker and NPC System
              modules to the prompt writer, so the same character looks the same across
              images. For an even stronger likeness, add a character LoRA in the LoRAs tab
              (a condition can limit it to scenes where that character appears).
            </p>
            <div>
              <label className={labelCls}>Player character in images</label>
              <select
                value={draft.player_in_images || 'show'}
                onChange={(e) => set('player_in_images', e.target.value)}
                disabled={draft.character_reference_enabled === false}
                className={`${inputCls} disabled:opacity-50`}
              >
                <option value="show">Show the player character</option>
                <option value="pov">Never depict the player — first-person POV only during direct interactions</option>
              </select>
            </div>
          </div>
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className={`text-xs uppercase tracking-wider ${promptStyle === 'natural' ? 'text-purple-400' : 'text-gray-500'}`}>
                Natural-language template (Flux &amp; general models){promptStyle === 'natural' ? ' — active' : ''}
              </label>
              <button
                onClick={() => set('prompt_template', config.default_prompt_template)}
                className="text-xs text-gray-500 hover:text-gray-300"
              >
                Reset to default
              </button>
            </div>
            <AutoGrowTextarea
              value={draft.prompt_template || ''}
              onChange={(e) => set('prompt_template', e.target.value)}
              rows={3}
              className={`${inputCls} font-mono text-xs leading-relaxed`}
            />
          </div>
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className={`text-xs uppercase tracking-wider ${promptStyle === 'tags' ? 'text-purple-400' : 'text-gray-500'}`}>
                Booru tag template (Pony / Illustrious / NoobAI){promptStyle === 'tags' ? ' — active' : ''}
              </label>
              <button
                onClick={() => set('prompt_template_tags', config.default_prompt_template_tags)}
                className="text-xs text-gray-500 hover:text-gray-300"
              >
                Reset to default
              </button>
            </div>
            <AutoGrowTextarea
              value={draft.prompt_template_tags || ''}
              onChange={(e) => set('prompt_template_tags', e.target.value)}
              rows={3}
              className={`${inputCls} font-mono text-xs leading-relaxed`}
            />
            <p className="text-xs text-gray-600 mt-1">
              Placeholders in both templates: <code className="text-purple-400">{'{narration}'}</code> = latest scene,{' '}
              <code className="text-purple-400">{'{history}'}</code> = earlier scenes.
            </p>
          </div>
          <div className="space-y-2">
            <label className={labelCls}>Tag models: characters per image</label>
            <select
              value={draft.booru_subject_mode || 'auto'}
              onChange={(e) => set('booru_subject_mode', e.target.value)}
              className={inputCls}
            >
              <option value="auto">Auto — solo scenes stay solo, group scenes get 2-3 characters</option>
              <option value="single">Single — always focus on the most relevant character</option>
              <option value="multi">Multi — always allow 2-3 characters with distinct looks</option>
            </select>
            <p className="text-xs text-gray-600">
              Tag checkpoints blend features together when several characters share one
              prompt. Single keeps the strongest results by tagging only the character
              the scene centers on (solo). Multi structures the prompt instead — a
              subject-count tag (2girls, 1boy 1girl...) and one contiguous tag group per
              character, led by their most distinguishing traits — so each character
              keeps their own look. Works best on Illustrious and NoobAI, less reliably
              on Pony. Natural-language models are unaffected.
            </p>
            {(draft.booru_subject_mode || 'auto') !== 'single' && (
              <>
                <Toggle
                  checked={draft.booru_break_separator === true}
                  onChange={(v) => set('booru_break_separator', v)}
                  label="Insert BREAK between character tag groups"
                />
                <p className="text-xs text-gray-600">
                  A1111-style prompt chunking that further isolates each character's
                  tags. Only useful if the generation pipeline honors the BREAK
                  keyword — leave off if unsure.
                </p>
              </>
            )}
          </div>
          <div className="space-y-2">
            <label className={labelCls}>Tag models: rare-tag filter</label>
            <select
              value={draft.tag_usage_filter || 'off'}
              onChange={(e) => set('tag_usage_filter', e.target.value)}
              className={inputCls}
            >
              <option value="off">Off — keep every tag the prompt writer produces</option>
              <option value="soft">Soft — drop known booru tags below the usage threshold</option>
              <option value="hard">Hard — also drop tags neither danbooru nor e621 knows</option>
            </select>
            {(draft.tag_usage_filter || 'off') !== 'off' && (
              <div>
                <label className={labelCls}>Minimum booru post count</label>
                <input
                  type="number"
                  min={0}
                  value={draft.tag_usage_min_count ?? 100}
                  onChange={(e) => set('tag_usage_min_count', parseInt(e.target.value, 10) || 0)}
                  className={inputCls}
                />
              </div>
            )}
            <p className="text-xs text-gray-600">
              Prunes rare or invented tags from tag-style prompts and cached character
              tags, using bundled danbooru + e621 tag dictionaries (a tag counts by its
              highest usage on either site, so anime and furry tags both work) — tags a
              checkpoint barely saw in training mostly add noise. Soft only removes tags
              the dictionaries know but that fall below the threshold; Hard also removes
              tags neither site has heard of (usually hallucinated). LoRA trigger words,
              score_ tags, and BREAK always survive. Natural-language models are
              unaffected.
            </p>
          </div>
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className={`text-xs uppercase tracking-wider ${qualityMarker ? 'text-purple-400' : 'text-gray-500'}`}>
                Quality tags (prepended for booru-tag models){qualityMarker ? ' — active' : ''}
              </label>
              <button
                onClick={() => set('quality_tags', qualityDefault)}
                className="text-xs text-gray-500 hover:text-gray-300"
              >
                Reset to default
              </button>
            </div>
            <AutoGrowTextarea
              value={draft.quality_tags || ''}
              onChange={(e) => set('quality_tags', e.target.value)}
              placeholder={qualityDefault}
              className={inputCls}
            />
            <p className="text-xs text-gray-600 mt-1">
              Each tag-trained family expects its own quality vocabulary — Pony wants
              score_* tags, NoobAI/Illustrious/Animagine masterpiece-style tags. Left
              at a stock value the field follows the checkpoint family automatically;
              edit it to pin your own tags for this profile.
            </p>
          </div>
          <div>
            <label className={labelCls}>Style suffix (appended to every image prompt)</label>
            <AutoGrowTextarea
              value={draft.style_suffix || ''}
              onChange={(e) => set('style_suffix', e.target.value)}
              placeholder="e.g. digital painting, dramatic lighting, fantasy concept art"
              className={inputCls}
            />
          </div>
          <div>
            <label className={labelCls}>Negative Prompt</label>
            <AutoGrowTextarea
              value={draft.negative_prompt || ''}
              onChange={(e) => set('negative_prompt', e.target.value)}
              placeholder="blurry, low quality, watermark, text"
              className={inputCls}
            />
          </div>
        </section>
        )}

        {/* LoRAs */}
        {tab === 'loras' && (
        <LoraSection
          config={config}
          draft={draft}
          set={set}
          library={library}
          setLibrary={setLibrary}
          checkpointFamily={checkpointFamily}
          onConfigRefresh={refreshLibrary}
        />
        )}

        {/* Test generate */}
        {tab === 'generate' && (
        <section className={sectionCls}>
          <h2 className="text-sm font-semibold text-gray-300">Test Generate</h2>
          <p className="text-xs text-gray-600">
            {testRefine
              ? 'Your text is treated as a scene: the prompt-writer LLM refines it with your templates, trigger words and style — exactly like a story illustration.'
              : 'Sends your text straight to the image model, word for word (skips the prompt-writer LLM).'}
            {(Number(draft.image_num) || 1) > 1 && (testRefine
              ? ' With several images per generation, the scene is split into beats and the images form a chronological sequence; each described character’s appearance is fixed up front so they look the same in every image.'
              : ' With several images per generation, all of them use this exact text — only the seed differs (refine to get a beat sequence instead).')}
            {' '}Uses saved settings — save changes first. Results appear in the Image Library tab.
          </p>
          <div className="flex items-start gap-2">
            <AutoGrowTextarea
              value={testPrompt}
              onChange={(e) => updateTestPrompt(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  if (testPrompt.trim() && !generating) testGenerate();
                }
              }}
              placeholder={testRefine
                ? 'The knight faces the dragon on the crumbling bridge at dawn'
                : 'A moonlit castle above a stormy sea, oil painting'}
              className={inputCls}
            />
            <button
              onClick={testGenerate}
              disabled={generating || !testPrompt.trim() || !config.has_key || !config.model_name}
              className="px-4 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed shrink-0 inline-flex items-center gap-2"
            >
              {generating && (
                <span className="h-3.5 w-3.5 rounded-full border-2 border-white/40 border-t-white animate-spin" />
              )}
              {generating ? 'Generating…' : 'Generate'}
            </button>
          </div>
          <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={testRefine}
              onChange={(e) => updateTestRefine(e.target.checked)}
              className="accent-purple-500"
            />
            Refine with the prompt-writer LLM (uncheck to send the text as-is)
          </label>
          {!isLocal && !config.has_key && (
            <p className="text-xs text-yellow-500">Save a Novita API key in the Setup tab first.</p>
          )}
          {(isLocal || config.has_key) && !config.model_name && (
            <p className="text-xs text-yellow-500">Pick a model in the Setup tab and save first.</p>
          )}
          {testError && <p className="text-xs text-red-400">{testError}</p>}
        </section>
        )}

        {/* Image Library — every generated image, with the chat conceal setting applied */}
        {tab === 'library' && (
        <section className={sectionCls}>
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-300">
              Image Library {records.length > 0 && <span className="text-gray-600">({records.length})</span>}
            </h2>
            {pendingCount > 0 && (
              <span className="text-xs text-purple-400 animate-pulse">
                {pendingCount} generating…
              </span>
            )}
          </div>
          <p className="text-xs text-gray-600">
            Every image generated for this world — story illustrations and studio tests alike.
            {conceal !== 'off'
              ? ` Images are ${conceal === 'blackout' ? 'blacked out' : 'blurred'} until clicked, matching the chat conceal setting (Output tab); the eye button hides one again.`
              : ' Set “Hide images in chat until clicked” in the Output tab to conceal them here too.'}
          </p>
          {records.length === 0 ? (
            <p className="text-sm text-gray-600 italic">No images yet.</p>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {records.flatMap((r) => {
                // A multi-image record gets one tile per image (and one
                // placeholder tile per expected image while generating);
                // deleting any of them removes the whole generation, since
                // they share a record. Conceal and reveal apply per image.
                const files = r.status === 'done' ? recordFiles(r) : [];
                const generating = ['pending', 'prompting', 'generating'].includes(r.status);
                const tiles = files.length > 0
                  ? files
                  : Array.from(
                      { length: generating ? Math.max(1, Number(r.image_num) || 1) : 1 },
                      () => null);
                return tiles.map((filename, ti) => {
                  const concealed =
                    !!filename && conceal !== 'off' && !revealed.has(filename);
                  return (
                  <div key={filename ? `${r.id}:${filename}` : `${r.id}:${ti}`} className="group relative bg-gray-950/60 border border-gray-800 rounded-lg overflow-hidden">
                    {filename ? (
                      <div className="relative overflow-hidden">
                        <img
                          src={`${API_BASE}/images/file/${filename}`}
                          alt={concealed ? 'Hidden image' : (r.image_prompt || '')}
                          title={`${r.model_name || ''}${r.loras?.length ? ` + LoRAs: ${r.loras.join(', ')}` : ''}`}
                          loading="lazy"
                          onClick={() => (concealed
                            ? revealImage(filename)
                            : setLightbox({ index: Math.max(0, galleryItems.findIndex((it) => it.filename === filename)) }))}
                          className={`w-full h-32 ${
                            concealed
                              ? conceal === 'blackout'
                                ? 'object-cover cursor-pointer brightness-0'
                                : 'object-cover cursor-pointer blur-2xl scale-110'
                              : 'object-contain cursor-zoom-in'
                          }`}
                        />
                        {concealed && (
                          <button
                            onClick={() => revealImage(filename)}
                            aria-label="Reveal image"
                            className="absolute inset-0 flex items-center justify-center cursor-pointer"
                          >
                            <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-black/60 border border-white/10 text-[10px] text-gray-300">
                              <span aria-hidden="true">👁</span> Click to reveal
                            </span>
                          </button>
                        )}
                        {!concealed && conceal !== 'off' && (
                          <button
                            onClick={() => hideImage(filename)}
                            title="Hide image"
                            aria-label="Hide image"
                            className="absolute top-1.5 left-1.5 px-1.5 py-1 rounded text-xs leading-none bg-black/60 text-gray-300 border border-white/10 hover:bg-black/80 hover:text-white transition-colors"
                          >
                            <span aria-hidden="true">👁</span>
                          </button>
                        )}
                      </div>
                    ) : r.status === 'error' ? (
                      <button
                        onClick={() => setLightbox({ record: r })}
                        title="Open to see why it failed"
                        className="w-full h-32 flex flex-col items-center justify-center gap-1 px-2 text-center text-xs text-red-300/80 cursor-pointer hover:bg-red-950/20 transition-colors"
                      >
                        <span className="text-lg" aria-hidden="true">🚫</span>
                        <span className="line-clamp-3">{r.error || 'failed'}</span>
                      </button>
                    ) : (
                      <div className="w-full h-32 flex items-center justify-center text-xs text-gray-600 px-2 text-center">
                        generating…
                      </div>
                    )}
                    <div className="px-2 py-1.5 flex items-center justify-between gap-1">
                      <div className="min-w-0">
                        <StatusBadge status={r.status} />
                        <span className="ml-1.5 text-[10px] text-gray-600 truncate">
                          {r.save_id === '__studio__' ? 'studio' : `${r.save_id} · t${r.turn}`}
                        </span>
                      </div>
                      <button
                        onClick={() => deleteRecord(r.id)}
                        className="text-gray-600 hover:text-red-400 transition-colors shrink-0"
                        title={files.length > 1 ? `Delete generation (${files.length} images)` : 'Delete'}
                      >
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                      </button>
                    </div>
                  </div>
                  );
                });
              })}
            </div>
          )}
        </section>
        )}
      </div>
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
