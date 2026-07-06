import React, { useState, useEffect, useCallback, useRef } from 'react';

const API_BASE = '/api/modules/wb_image_gen';

const inputCls =
  'w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 ' +
  'focus:border-purple-500 focus:outline-none placeholder-gray-600';
const labelCls = 'block text-xs uppercase tracking-wider text-gray-500 mb-1.5';
const sectionCls = 'bg-gray-900/60 border border-gray-800 rounded-xl p-5 space-y-4';

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

function Lightbox({ record, onClose }) {
  return (
    <div
      className="fixed inset-0 z-50 bg-black/90 flex flex-col items-center justify-center p-4 cursor-zoom-out"
      onClick={onClose}
    >
      <img
        src={`${API_BASE}/images/file/${record.filename}`}
        alt={record.image_prompt || 'Generated image'}
        className="max-w-full max-h-[85vh] rounded-lg shadow-2xl"
      />
      {record.image_prompt && (
        <p className="mt-3 max-w-2xl text-center text-xs text-gray-400">{record.image_prompt}</p>
      )}
    </div>
  );
}

// Searchable dropdown over Novita's checkpoint catalog (thousands of models).
// Searches server-side via the module's /models proxy; cursor-paginated.
// onSelect receives the whole model object (sd_name + base_model metadata).
function ModelPicker({ value, valueBase, hasKey, onSelect }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [models, setModels] = useState([]);
  const [nextCursor, setNextCursor] = useState('');
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
              onClick={() => search(query, nextCursor)}
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

function LoraRow({ entry, checkpointFamily, onPatch, onDelete, onRematch }) {
  const [strength, setStrength] = useState(entry.strength ?? 0.7);
  const [showOverride, setShowOverride] = useState(false);
  const [override, setOverride] = useState(entry.sd_name_override || '');
  const [busy, setBusy] = useState(false);

  useEffect(() => { setStrength(entry.strength ?? 0.7); }, [entry.strength]);

  const fam = baseFamily(entry.base_model);
  const availability = loraAvailability(entry);
  const compatible = fam && checkpointFamily && fam === checkpointFamily;
  const dimmed = entry.active && !compatible;

  return (
    <div className={`bg-gray-950/60 border border-gray-800 rounded-lg p-3 space-y-2 ${!compatible ? 'opacity-60' : ''}`}>
      <div className="flex items-center gap-3">
        {entry.thumb_url ? (
          <img src={entry.thumb_url} alt="" loading="lazy" className="w-10 h-10 rounded object-cover shrink-0 bg-gray-800" />
        ) : (
          <div className="w-10 h-10 rounded bg-gray-800 shrink-0" />
        )}
        <div className="min-w-0 flex-1">
          <a
            href={entry.civitai_url}
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
          <label className="text-[10px] uppercase tracking-wider text-gray-500 shrink-0">
            Strength: {Number(strength).toFixed(2)}
          </label>
          <input
            type="range" min={0} max={1} step={0.05}
            value={strength}
            onChange={(e) => setStrength(Number(e.target.value))}
            onMouseUp={() => onPatch(entry.id, { strength })}
            onTouchEnd={() => onPatch(entry.id, { strength })}
            onKeyUp={() => onPatch(entry.id, { strength })}
            className="w-full accent-purple-500"
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
            <button onClick={() => setShowOverride((s) => !s)} className="text-gray-500 hover:text-gray-300">
              {showOverride ? 'Hide manual name' : 'Enter name manually'}
            </button>
            <a
              href="https://novita.ai/models-console/model-management"
              target="_blank" rel="noreferrer"
              className="text-gray-500 hover:text-gray-300 underline"
            >
              Upload on Novita (5 free slots)
            </a>
          </div>
          {showOverride && (
            <div className="flex gap-2">
              <input
                type="text"
                value={override}
                onChange={(e) => setOverride(e.target.value)}
                placeholder="MODEL NAME IN API from your Novita private model list"
                className={`${inputCls} text-xs`}
              />
              <button
                onClick={() => onPatch(entry.id, { sd_name_override: override })}
                className="px-3 py-1 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-xs shrink-0"
              >
                Set
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Civitai LoRA browser + local library. Browsing is proxied through the module
// backend (which injects the Civitai key for NSFW); saving stores metadata only
// — no file ever touches this device.
function LoraSection({ config, draft, set, library, setLibrary, checkpointFamily }) {
  const [query, setQuery] = useState('');
  const [baseModel, setBaseModel] = useState('');
  const [loraType, setLoraType] = useState('LORA');
  const [sort, setSort] = useState('Most Downloaded');
  const [items, setItems] = useState([]);
  const [nextCursor, setNextCursor] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(false);
  const debounceRef = useRef(null);
  const seqRef = useRef(0);

  const nsfw = !!draft.civitai_nsfw;
  const savedIds = new Set(library.map((e) => e.id));

  const search = useCallback(async (cursor = '') => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError('');
    try {
      const params = new URLSearchParams({ query, lora_type: loraType, sort, nsfw: String(nsfw) });
      if (baseModel) params.set('base_model', baseModel);
      if (cursor) params.set('cursor', cursor);
      const res = await fetch(`${API_BASE}/civitai/loras?${params}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (seq !== seqRef.current) return;
      setItems((prev) => (cursor ? [...prev, ...data.items] : data.items));
      setNextCursor(data.next_cursor || '');
    } catch (e) {
      if (seq === seqRef.current) setError(String(e.message || e));
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [query, baseModel, loraType, sort, nsfw]);

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
        <button
          onClick={() => setOpen((o) => !o)}
          className="text-xs text-purple-400 hover:text-purple-300"
        >
          {open ? 'Close browser' : 'Browse Civitai…'}
        </button>
      </div>
      <p className="text-xs text-gray-600">
        Save LoRAs you like from Civitai, then activate them. SD-family LoRAs are applied through Novita's
        mirrored catalog; Flux LoRAs are sent as download links (FLUX.2 model only). Active LoRAs that do not
        match the selected checkpoint are skipped.
      </p>

      {open && (
        <div className="space-y-3">
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
              {(config.civitai_base_models || []).map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
            <select value={loraType} onChange={(e) => setLoraType(e.target.value)} className={inputCls}>
              {(config.civitai_lora_types || ['LORA']).map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <select value={sort} onChange={(e) => setSort(e.target.value)} className={inputCls}>
              {(config.civitai_sorts || []).map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div className="flex items-center gap-4">
            <Toggle
              checked={nsfw}
              onChange={(v) => {
                if (v && !config.has_civitai_key) return;
                set('civitai_nsfw', v);
              }}
              label={<span className={config.has_civitai_key ? '' : 'opacity-50'}>Include NSFW</span>}
            />
            {!config.has_civitai_key && (
              <span className="text-[11px] text-yellow-600">Save a Civitai API key to browse NSFW LoRAs.</span>
            )}
          </div>

          {error && <p className="text-xs text-red-400">{error}</p>}

          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 max-h-96 overflow-y-auto pr-1">
            {items.map((item) => (
              <div key={item.id} className="bg-gray-950/60 border border-gray-800 rounded-lg overflow-hidden">
                {item.thumb_url ? (
                  <img src={item.thumb_url} alt="" loading="lazy" className="w-full h-28 object-cover bg-gray-800" />
                ) : (
                  <div className="w-full h-28 bg-gray-800" />
                )}
                <div className="p-2 space-y-1">
                  <a
                    href={item.civitai_url} target="_blank" rel="noreferrer"
                    className="text-xs text-gray-200 hover:text-purple-300 line-clamp-2 leading-snug"
                    title={item.name}
                  >
                    {item.name}
                  </a>
                  <div className="flex items-center justify-between text-[10px] text-gray-500">
                    <span className="truncate">{item.base_model}</span>
                    <span className="shrink-0">⬇ {fmtCount(item.stats?.downloads)} · 👍 {fmtCount(item.stats?.likes)}</span>
                  </div>
                  <button
                    onClick={() => saveLora(item)}
                    disabled={savedIds.has(item.id)}
                    className="w-full px-2 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white text-[11px] font-medium disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {savedIds.has(item.id) ? 'Saved ✓' : 'Save to library'}
                  </button>
                </div>
              </div>
            ))}
          </div>
          {loading && <p className="text-xs text-gray-500 animate-pulse">Searching Civitai…</p>}
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
        <p className="text-sm text-gray-600 italic">No saved LoRAs yet — browse Civitai to add some.</p>
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
  const [keyInput, setKeyInput] = useState('');
  const [civitaiKeyInput, setCivitaiKeyInput] = useState('');
  const [library, setLibrary] = useState([]);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [savedFlash, setSavedFlash] = useState(false);

  const [records, setRecords] = useState([]);
  const [pendingCount, setPendingCount] = useState(0);
  const [lightbox, setLightbox] = useState(null);

  const [testPrompt, setTestPrompt] = useState('');
  const [testError, setTestError] = useState('');
  const pollRef = useRef(null);

  const loadConfig = useCallback(async () => {
    const res = await fetch(`${API_BASE}/config`);
    const data = await res.json();
    setConfig(data);
    setDraft(data);
    setLibrary(data.lora_library || []);
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

  const dirty =
    config &&
    (keyInput.trim() !== '' ||
      civitaiKeyInput.trim() !== '' ||
      JSON.stringify({ ...draft, api_key: '', civitai_api_key: '', lora_library: [] }) !==
        JSON.stringify({ ...config, api_key: '', civitai_api_key: '', lora_library: [] }));

  // Mirror of the backend's _prompt_style heuristic, live against the draft.
  const modelIdent = `${draft.model_base || ''} ${draft.model_name || ''}`.toLowerCase();
  const promptStyle = modelIdent.includes('pony') || modelIdent.includes('illustrious') ? 'tags' : 'natural';
  const isPony = modelIdent.includes('pony');
  const checkpointFamily =
    draft.model_name === config?.flux2_model_name ? 'flux' : baseFamily(modelIdent);

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
        steps: Number(draft.steps) || 28,
        guidance_scale: Number(draft.guidance_scale) || 7,
        sampler_name: draft.sampler_name,
        negative_prompt: draft.negative_prompt,
        interval: Number(draft.interval) || 3,
        prompt_model_preference: draft.prompt_model_preference,
        prompt_template: draft.prompt_template,
        prompt_template_tags: draft.prompt_template_tags,
        pony_quality_tags: draft.pony_quality_tags,
        style_suffix: draft.style_suffix,
        civitai_nsfw: !!draft.civitai_nsfw,
      };
      if (keyInput.trim()) payload.api_key = keyInput.trim();
      if (civitaiKeyInput.trim()) payload.civitai_api_key = civitaiKeyInput.trim();
      const res = await fetch(`${API_BASE}/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setConfig(data);
      setDraft(data);
      setLibrary(data.lora_library || []);
      setKeyInput('');
      setCivitaiKeyInput('');
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
    try {
      const res = await fetch(`${API_BASE}/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt_override: testPrompt.trim(), save_id: '__studio__' }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      setPendingCount((c) => c + 1);
      loadImages();
    } catch (e) {
      setTestError(String(e.message || e));
    }
  };

  const deleteRecord = async (recordId) => {
    await fetch(`${API_BASE}/images/${recordId}`, { method: 'DELETE' }).catch(() => {});
    loadImages();
  };

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
      <div className="max-w-3xl mx-auto px-6 py-8 space-y-6">
        <div className="flex items-center justify-between">
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

        <div>
          <h1 className="text-3xl font-bold flex items-center gap-3">🎨 Image Studio</h1>
          <p className="text-gray-500 mt-1 text-sm">
            Story illustrations via the Novita AI API. Auto-generates every N turns; use /image in-game for on-demand shots.
          </p>
        </div>

        {saveError && (
          <div className="bg-red-950/50 border border-red-900 rounded-lg px-4 py-2 text-sm text-red-200">
            {saveError}
          </div>
        )}

        {/* Connection */}
        <section className={sectionCls}>
          <h2 className="text-sm font-semibold text-gray-300">Connection</h2>
          <div>
            <label className={labelCls}>Novita API Key</label>
            <input
              type="password"
              value={keyInput}
              onChange={(e) => setKeyInput(e.target.value)}
              placeholder={config.has_key ? `Saved (${config.api_key}) — type to replace` : 'Paste your novita.ai key'}
              className={inputCls}
              autoComplete="off"
            />
            <p className="text-xs text-gray-600 mt-1">
              Get a key at{' '}
              <a href="https://novita.ai/settings/key-management" target="_blank" rel="noreferrer" className="text-purple-400 hover:underline">
                novita.ai
              </a>
              . Stored locally on this machine only.
            </p>
          </div>
          <div>
            <label className={labelCls}>Civitai API Key (optional)</label>
            <input
              type="password"
              value={civitaiKeyInput}
              onChange={(e) => setCivitaiKeyInput(e.target.value)}
              placeholder={config.has_civitai_key ? `Saved (${config.civitai_api_key}) — type to replace` : 'Paste your civitai.com key'}
              className={inputCls}
              autoComplete="off"
            />
            <p className="text-xs text-gray-600 mt-1">
              Needed for NSFW LoRA browsing and for FLUX.2 LoRA download links (Civitai requires auth on
              downloads). Create one under{' '}
              <a href="https://civitai.com/user/account" target="_blank" rel="noreferrer" className="text-purple-400 hover:underline">
                civitai.com account settings
              </a>
              . Stored locally on this machine only.
            </p>
          </div>
          <ModelPicker
            value={draft.model_name}
            valueBase={draft.model_base}
            hasKey={!!config.has_key}
            onSelect={(m) => setDraft((d) => ({ ...d, model_name: m.sd_name, model_base: m.base_model || '' }))}
          />
        </section>

        {/* Output */}
        <section className={sectionCls}>
          <h2 className="text-sm font-semibold text-gray-300">Output</h2>
          <Toggle
            checked={!!draft.enabled}
            onChange={(v) => set('enabled', v)}
            label="Auto-illustrate the story"
          />
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

        {/* Prompting */}
        <section className={sectionCls}>
          <h2 className="text-sm font-semibold text-gray-300">Prompting</h2>
          {draft.model_name && (
            <div className="text-xs text-gray-500 bg-gray-950/60 border border-gray-800 rounded-lg px-3 py-2">
              Selected model uses the{' '}
              <span className="text-purple-300 font-medium">
                {promptStyle === 'tags' ? 'danbooru tag' : 'natural language'}
              </span>{' '}
              template{isPony ? ', with Pony quality tags prepended' : ''}. Detected from base model
              {draft.model_base ? ` "${draft.model_base}"` : ' / model name'} — Flux → natural language, Pony/Illustrious → tags.
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
                Danbooru tag template (Pony / Illustrious){promptStyle === 'tags' ? ' — active' : ''}
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

        {/* LoRAs */}
        <LoraSection
          config={config}
          draft={draft}
          set={set}
          library={library}
          setLibrary={setLibrary}
          checkpointFamily={checkpointFamily}
        />

        {/* Test generate */}
        <section className={sectionCls}>
          <h2 className="text-sm font-semibold text-gray-300">Test Generate</h2>
          <p className="text-xs text-gray-600">
            Sends your text straight to the image model (skips the prompt-writer LLM). Uses saved settings — save changes first.
          </p>
          <div className="flex gap-2">
            <input
              type="text"
              value={testPrompt}
              onChange={(e) => setTestPrompt(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && testPrompt.trim()) testGenerate(); }}
              placeholder="A moonlit castle above a stormy sea, oil painting"
              className={inputCls}
            />
            <button
              onClick={testGenerate}
              disabled={!testPrompt.trim() || !config.has_key || !config.model_name}
              className="px-4 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
            >
              Generate
            </button>
          </div>
          {!config.has_key && <p className="text-xs text-yellow-500">Save an API key first.</p>}
          {config.has_key && !config.model_name && (
            <p className="text-xs text-yellow-500">Pick a model and save first.</p>
          )}
          {testError && <p className="text-xs text-red-400">{testError}</p>}
        </section>

        {/* Gallery */}
        <section className={sectionCls}>
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-300">
              Gallery {records.length > 0 && <span className="text-gray-600">({records.length})</span>}
            </h2>
            {pendingCount > 0 && (
              <span className="text-xs text-purple-400 animate-pulse">
                {pendingCount} generating…
              </span>
            )}
          </div>
          {records.length === 0 ? (
            <p className="text-sm text-gray-600 italic">No images yet.</p>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {records.map((r) => (
                <div key={r.id} className="group relative bg-gray-950/60 border border-gray-800 rounded-lg overflow-hidden">
                  {r.status === 'done' && r.filename ? (
                    <img
                      src={`${API_BASE}/images/file/${r.filename}`}
                      alt={r.image_prompt || ''}
                      title={`${r.model_name || ''}${r.loras?.length ? ` + LoRAs: ${r.loras.join(', ')}` : ''}`}
                      loading="lazy"
                      onClick={() => setLightbox(r)}
                      className="w-full h-32 object-cover cursor-zoom-in"
                    />
                  ) : (
                    <div className="w-full h-32 flex items-center justify-center text-xs text-gray-600 px-2 text-center">
                      {r.status === 'error' ? (r.error || 'failed') : 'generating…'}
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
                      title="Delete"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
      {lightbox && <Lightbox record={lightbox} onClose={() => setLightbox(null)} />}
    </div>
  );
}
