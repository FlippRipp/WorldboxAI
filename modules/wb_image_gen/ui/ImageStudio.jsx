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
          className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${
            checked ? 'translate-x-4.5 left-0.5' : 'left-0.5'
          }`}
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

export default function ImageStudio({ onBack }) {
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState({});
  const [keyInput, setKeyInput] = useState('');
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
      JSON.stringify({ ...draft, api_key: '' }) !== JSON.stringify({ ...config, api_key: '' }));

  const set = (key, value) => setDraft((d) => ({ ...d, [key]: value }));

  const save = async () => {
    setSaving(true);
    setSaveError('');
    try {
      const payload = {
        enabled: draft.enabled,
        endpoint: draft.endpoint,
        size_mode: draft.size_mode,
        aspect_ratio: draft.aspect_ratio,
        width: Number(draft.width) || 1024,
        height: Number(draft.height) || 768,
        interval: Number(draft.interval) || 3,
        prompt_model_preference: draft.prompt_model_preference,
        prompt_template: draft.prompt_template,
        style_suffix: draft.style_suffix,
      };
      if (keyInput.trim()) payload.api_key = keyInput.trim();
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
      setKeyInput('');
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
            FLUX.2 story illustrations via the Black Forest Labs API. Auto-generates every N turns; use /image in-game for on-demand shots.
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
            <label className={labelCls}>BFL API Key</label>
            <input
              type="password"
              value={keyInput}
              onChange={(e) => setKeyInput(e.target.value)}
              placeholder={config.has_key ? `Saved (${config.api_key}) — type to replace` : 'Paste your api.bfl.ai key'}
              className={inputCls}
              autoComplete="off"
            />
            <p className="text-xs text-gray-600 mt-1">
              Get a key at{' '}
              <a href="https://dashboard.bfl.ai" target="_blank" rel="noreferrer" className="text-purple-400 hover:underline">
                dashboard.bfl.ai
              </a>
              . Stored locally on this machine only.
            </p>
          </div>
          <div>
            <label className={labelCls}>Model Endpoint</label>
            <select
              value={draft.endpoint}
              onChange={(e) => set('endpoint', e.target.value)}
              className={inputCls}
            >
              {(config.endpoints || []).map((ep) => (
                <option key={ep} value={ep}>{ep}</option>
              ))}
            </select>
          </div>
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
            <label className={labelCls}>Image Size</label>
            <div className="flex gap-2 mb-2">
              <button
                onClick={() => set('size_mode', 'aspect')}
                className={`px-3 py-1.5 rounded-lg text-xs border transition-colors ${
                  draft.size_mode !== 'explicit'
                    ? 'bg-purple-600/30 border-purple-600 text-purple-200'
                    : 'bg-gray-800 border-gray-700 text-gray-400 hover:text-gray-200'
                }`}
              >
                Aspect ratio
              </button>
              <button
                onClick={() => set('size_mode', 'explicit')}
                className={`px-3 py-1.5 rounded-lg text-xs border transition-colors ${
                  draft.size_mode === 'explicit'
                    ? 'bg-purple-600/30 border-purple-600 text-purple-200'
                    : 'bg-gray-800 border-gray-700 text-gray-400 hover:text-gray-200'
                }`}
              >
                Exact pixels
              </button>
            </div>
            {draft.size_mode === 'explicit' ? (
              <div className="flex gap-3 items-center">
                <input
                  type="number" min={256} max={2048} step={32}
                  value={draft.width || 1024}
                  onChange={(e) => set('width', e.target.value)}
                  className={inputCls}
                />
                <span className="text-gray-600">×</span>
                <input
                  type="number" min={256} max={2048} step={32}
                  value={draft.height || 768}
                  onChange={(e) => set('height', e.target.value)}
                  className={inputCls}
                />
              </div>
            ) : (
              <select
                value={draft.aspect_ratio || '16:9'}
                onChange={(e) => set('aspect_ratio', e.target.value)}
                className={inputCls}
              >
                {(config.aspect_ratios || []).map((ar) => (
                  <option key={ar} value={ar}>{ar}</option>
                ))}
              </select>
            )}
          </div>
        </section>

        {/* Prompting */}
        <section className={sectionCls}>
          <h2 className="text-sm font-semibold text-gray-300">Prompting</h2>
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
              The LLM that turns the latest scene into an image prompt before FLUX is called.
            </p>
          </div>
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="text-xs uppercase tracking-wider text-gray-500">
                Prompt-writer template
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
              rows={10}
              className={`${inputCls} font-mono text-xs leading-relaxed`}
            />
            <p className="text-xs text-gray-600 mt-1">
              Placeholders: <code className="text-purple-400">{'{narration}'}</code> = latest scene,{' '}
              <code className="text-purple-400">{'{history}'}</code> = earlier scenes.
            </p>
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

        {/* Test generate */}
        <section className={sectionCls}>
          <h2 className="text-sm font-semibold text-gray-300">Test Generate</h2>
          <p className="text-xs text-gray-600">
            Sends your text straight to FLUX (skips the prompt-writer LLM). Uses saved settings — save changes first.
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
              disabled={!testPrompt.trim() || !config.has_key}
              className="px-4 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
            >
              Generate
            </button>
          </div>
          {!config.has_key && <p className="text-xs text-yellow-500">Save an API key first.</p>}
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
