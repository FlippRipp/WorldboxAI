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

// Single-line-height textarea that grows with its content, so longer text
// stays readable while typing. Modeled on the CharacterBuilder's AutoTextarea.
function AutoGrowTextarea({ value, onChange, onBlur, onKeyDown, placeholder, className }) {
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
      rows={1}
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
function ModelPicker({ value, valueBase, hasKey, onSelect }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [models, setModels] = useState([]);
  const [nextCursor, setNextCursor] = useState('');
  const [effQuery, setEffQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const debounceRef = useRef(null);
  const seqRef = useRef(0);
  const boxRef = useRef(null);

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
              disabled={!hasKey}
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
          disabled={!hasKey}
          onChange={(e) => setQuery(e.target.value)}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false); }}
          placeholder={hasKey ? 'Search thousands of models — e.g. realistic, anime, fantasy…' : 'Save an API key first'}
          className={inputCls}
        />
      )}
      {!hasKey && !value && (
        <p className="text-xs text-yellow-500 mt-1">Save an API key to browse models.</p>
      )}

      {open && (
        <div className="absolute z-30 mt-1 w-full max-h-80 overflow-y-auto bg-gray-900 border border-gray-700 rounded-lg shadow-2xl">
          {error && <div className="px-3 py-2 text-xs text-red-400">{error}</div>}
          {!error && models.length === 0 && !loading && (
            <div className="px-3 py-2 text-xs text-gray-500 italic">No models found.</div>
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

// Availability of a saved LoRA on Novita: Flux LoRAs travel as download links,
// SD-family ones must exist in Novita's mirrored catalog (or be console-uploaded
// and named manually).
function loraAvailability(entry) {
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

// Availability badge for a browse result (not yet saved). The backend checks
// each result's hashes against Novita's mirrored catalog; flux LoRAs travel
// as download links so the mirror doesn't matter for them.
function browseAvailability(item) {
  if (item.gated) {
    return {
      label: 'gated', cls: 'bg-yellow-900/40 text-yellow-400 border-yellow-800',
      title: 'Gated Hugging Face repo — Novita cannot download it, so it cannot be used',
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

function LoraRow({ entry, checkpointFamily, onPatch, onDelete, onRematch, myLoras, maxSlots, loadMyUploads, fetchMyUploads }) {
  const [strength, setStrength] = useState(entry.strength ?? 0.7);
  const [showOverride, setShowOverride] = useState(false);
  const [override, setOverride] = useState(entry.sd_name_override || '');
  const [busy, setBusy] = useState(false);
  const [detected, setDetected] = useState(null); // new upload seen but still processing
  const [condition, setCondition] = useState(entry.condition || '');
  const [triggers, setTriggers] = useState((entry.trained_words || []).join(', '));

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
  const availability = loraAvailability(entry);
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
        <div className="flex items-center gap-2">
          <label
            className="text-[10px] uppercase tracking-wider shrink-0 text-gray-500"
            title="Trigger words woven into every prompt that uses this LoRA. Pulled from the model page — edit them here if they're wrong or missing. Comma-separated."
          >
            Triggers
          </label>
          <input
            type="text"
            value={triggers}
            onChange={(e) => setTriggers(e.target.value)}
            onBlur={commitTriggers}
            onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur(); }}
            placeholder="none — comma-separated words injected into the prompt (e.g. glowing runes, ornate armor)"
            className={`${inputCls} text-xs`}
          />
        </div>
      )}

      {!availability.ok && fam !== 'flux' && (
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
              <div className="flex gap-2">
                <input
                  type="text"
                  value={override}
                  onChange={(e) => setOverride(e.target.value)}
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

// LoRA browser (Civitai + Hugging Face) + local library. Browsing is proxied
// through the module backend (which injects the Civitai key for NSFW); saving
// stores metadata only — no file ever touches this device.
function LoraSection({ config, draft, set, library, setLibrary, checkpointFamily }) {
  const [source, setSource] = useState('civitai');
  const [novitaOnly, setNovitaOnly] = useState(false);
  const [query, setQuery] = useState('');
  const [baseModel, setBaseModel] = useState('');
  const [loraType, setLoraType] = useState('LORA');
  const [category, setCategory] = useState('');
  const [sort, setSort] = useState('Most Downloaded');
  const [items, setItems] = useState([]);
  const [nextCursor, setNextCursor] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(false);
  const [recheckBusy, setRecheckBusy] = useState(false);
  const [myLoras, setMyLoras] = useState(null); // null = not fetched yet
  const [maxSlots, setMaxSlots] = useState(5);
  const debounceRef = useRef(null);
  const seqRef = useRef(0);
  const myLorasRef = useRef(false);
  const autoRecheckRef = useRef(false);

  // Hugging Face browsing is public, so the NSFW filter needs no key there;
  // Civitai requires its key for anything beyond SFW.
  const nsfwMode = (source === 'hf' || config.has_civitai_key) ? (draft.civitai_nsfw || 'off') : 'off';

  // "On Novita only": keep results that are usable as-is — hash-matched in
  // Novita's mirror, or flux (sent as a download link, no mirror needed).
  // Unknown availability (badge "?") is hidden too: it is not a known yes.
  const usableOnNovita = (i) =>
    !i.gated && (i.novita_available === true ||
      (baseFamily(i.base_model) === 'flux' && i.download_url));
  const visibleItems = novitaOnly ? items.filter(usableOnNovita) : items;
  const savedIds = new Set(library.map((e) => e.id));
  const isUnmatched = (e) =>
    baseFamily(e.base_model) !== 'flux' &&
    !(e.novita && e.novita.sd_name_in_api) &&
    !e.sd_name_override;
  const unmatchedCount = library.filter(isUnmatched).length;

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
      const res = await fetch(`${API_BASE}/${source === 'hf' ? 'hf' : 'civitai'}/loras?${params}`);
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
      if (seq === seqRef.current) setLoading(false);
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
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => search(), 400);
    return () => clearTimeout(debounceRef.current);
  }, [open, search]);

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
      await callLibrary('/loras/match_all', { method: 'POST' });
    } finally {
      setRecheckBusy(false);
    }
  };

  // Novita's mirror grows over time — silently recheck unmatched entries
  // once per studio visit when the last check is a week old or older.
  useEffect(() => {
    if (autoRecheckRef.current || !config.has_key) return;
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
          {unmatchedCount > 0 && config.has_key && (
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
        Save LoRAs you like from Civitai or Hugging Face, then activate them. SD-family LoRAs are applied
        through Novita's mirrored catalog; Flux LoRAs are sent as download links (FLUX.2 model only). Active
        LoRAs that do not match the selected checkpoint are skipped.
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
              title="Only show LoRAs that work right away: mirrored in Novita's catalog, or Flux LoRAs sent as download links. Hides ones with unknown availability."
            >
              <input
                type="checkbox"
                checked={novitaOnly}
                onChange={(e) => setNovitaOnly(e.target.checked)}
                className="accent-purple-500"
              />
              On Novita only
            </label>
            {source === 'civitai' && !config.has_civitai_key && (
              <span className="text-[11px] text-yellow-600">Save a Civitai API key (Setup tab) to browse NSFW LoRAs.</span>
            )}
          </div>

          {error && <p className="text-xs text-red-400">{error}</p>}

          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 max-h-96 overflow-y-auto pr-1">
            {visibleItems.map((item) => {
              const pageUrl = loraLink(item);
              const badge = browseAvailability(item);
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
                    <button
                      onClick={() => saveLora(item)}
                      disabled={savedIds.has(item.id) || item.gated}
                      title={item.gated ? 'Gated repos cannot be fetched by Novita' : undefined}
                      className="w-full px-2 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white text-[11px] font-medium disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {savedIds.has(item.id) ? 'Saved ✓' : item.gated ? 'Gated' : 'Save to library'}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
          {loading && <p className="text-xs text-gray-500 animate-pulse">Searching {source === 'hf' ? 'Hugging Face' : 'Civitai'}…</p>}
          {!loading && novitaOnly && items.length > visibleItems.length && (
            <p className="text-xs text-gray-600">
              {items.length - visibleItems.length} of {items.length} results hidden (not on Novita)
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
  const isPony = modelIdent.includes('pony');
  const checkpointFamily =
    draft.model_name === config?.flux2_model_name ? 'flux' : baseFamily(modelIdent);
  const activeLoraCount = library.filter(
    (e) => e.active && baseFamily(e.base_model) === checkpointFamily).length;

  const set = (key, value) => setDraft((d) => ({ ...d, [key]: value }));

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
        prompt_template: draft.prompt_template,
        prompt_template_tags: draft.prompt_template_tags,
        pony_quality_tags: draft.pony_quality_tags,
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
            Story illustrations via the Novita AI API. Auto-generates every N turns; use /image in-game for on-demand shots.
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

        {/* Setup: keys + model */}
        {tab === 'setup' && (
        <section className={sectionCls}>
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
          <KeyField
            provider="civitai"
            label="Civitai API Key (optional)"
            placeholder="Paste your civitai.com key"
            saved={!!config.has_civitai_key}
            savedMask={config.civitai_api_key}
            onSaved={(data) => { setConfig(data); setLibrary(data.lora_library || []); }}
          >
            <p className="text-xs text-gray-600 mt-1">
              Needed for NSFW LoRA browsing and for FLUX.2 LoRA download links (Civitai requires auth on
              downloads). Create one under{' '}
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
            onSelect={(m) => setDraft((d) => ({ ...d, model_name: m.sd_name, model_base: m.base_model || '' }))}
          />
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
              New illustrations in the story arrive covered; click one to reveal it.
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
              Each image gets its own AI-written prompt — a different angle,
              beat, or focus of the same scene — and its own Novita
              generation, all run in parallel (cost scales with the count).
              Swipe or use the arrow keys in the fullscreen viewer to flip
              through them.
            </p>
          </div>
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
              {(config.samplers || []).map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelCls}>Negative Prompt</label>
            <input
              type="text"
              value={draft.negative_prompt || ''}
              onChange={(e) => set('negative_prompt', e.target.value)}
              placeholder="blurry, low quality, watermark, text"
              className={inputCls}
            />
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
              template{isPony ? ', with Pony quality tags prepended' : ''}.
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
              The LLM that turns the latest scene into an image prompt before Novita is called.
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
            <textarea
              value={draft.prompt_template || ''}
              onChange={(e) => set('prompt_template', e.target.value)}
              rows={8}
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
            <textarea
              value={draft.prompt_template_tags || ''}
              onChange={(e) => set('prompt_template_tags', e.target.value)}
              rows={8}
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
              <label className={`text-xs uppercase tracking-wider ${isPony ? 'text-purple-400' : 'text-gray-500'}`}>
                Pony quality tags (prepended for Pony models){isPony ? ' — active' : ''}
              </label>
              <button
                onClick={() => set('pony_quality_tags', config.default_pony_quality_tags)}
                className="text-xs text-gray-500 hover:text-gray-300"
              >
                Reset to default
              </button>
            </div>
            <input
              type="text"
              value={draft.pony_quality_tags || ''}
              onChange={(e) => set('pony_quality_tags', e.target.value)}
              placeholder="score_9, score_8_up, score_7_up"
              className={inputCls}
            />
          </div>
          <div>
            <label className={labelCls}>Style suffix (appended to every image prompt)</label>
            <input
              type="text"
              value={draft.style_suffix || ''}
              onChange={(e) => set('style_suffix', e.target.value)}
              placeholder="e.g. digital painting, dramatic lighting, fantasy concept art"
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
            {' '}Uses saved settings — save changes first. Results appear in the Image Library tab.
          </p>
          <div className="flex gap-2">
            <input
              type="text"
              value={testPrompt}
              onChange={(e) => updateTestPrompt(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && testPrompt.trim() && !generating) testGenerate(); }}
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
          {!config.has_key && (
            <p className="text-xs text-yellow-500">Save a Novita API key in the Setup tab first.</p>
          )}
          {config.has_key && !config.model_name && (
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
              ? ` Images are ${conceal === 'blackout' ? 'blacked out' : 'blurred'} until clicked, matching the chat conceal setting (Output tab).`
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
