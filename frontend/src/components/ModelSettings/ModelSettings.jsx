import { useState, useEffect, useCallback, useMemo } from 'react';
import { api } from '../../lib/api';

const SELECT_CLASS = "w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-gray-200 text-sm focus:border-purple-500 focus:outline-none";
const BTN_PRIMARY = "px-4 py-2 bg-purple-600 hover:bg-purple-500 rounded-lg text-sm font-medium transition-colors disabled:opacity-50";

function FieldInput({ fieldKey, fdef, value, onChange, disabled }) {
  const label = fdef?.label || fieldKey;
  const desc = fdef?.description || '';

  if (fdef?.type === 'slider') {
    return (
      <div>
        <div className="flex justify-between text-sm">
          <span className="text-gray-300">{label}</span>
          <span className="text-purple-400 font-mono">{value ?? fdef.default}</span>
        </div>
        {desc && <p className="text-xs text-gray-500">{desc}</p>}
        <input
          type="range"
          min={fdef.min ?? 0}
          max={fdef.max ?? 100}
          step={fdef.step ?? 1}
          value={value ?? fdef.default ?? 0}
          onChange={(e) => onChange(fieldKey, Number(e.target.value))}
          disabled={disabled}
          className="w-full accent-purple-500"
        />
      </div>
    );
  }

  if (fdef?.type === 'toggle') {
    return (
      <div className="flex items-center justify-between">
        <div>
          <span className="text-sm text-gray-300">{label}</span>
          {desc && <p className="text-xs text-gray-500">{desc}</p>}
        </div>
        <button
          onClick={() => onChange(fieldKey, !value)}
          disabled={disabled}
          className={`w-10 h-5 rounded-full transition-colors ${value ? 'bg-purple-500' : 'bg-gray-600'}`}
        >
          <div className={`w-4 h-4 bg-white rounded-full transition-transform ${value ? 'translate-x-5' : 'translate-x-0.5'}`} />
        </button>
      </div>
    );
  }

  return (
    <div>
      <label className="text-sm text-gray-300">{label}</label>
      {desc && <p className="text-xs text-gray-500 mb-1">{desc}</p>}
      <input
        type={fdef?.type === 'secret' ? 'password' : 'text'}
        value={value || ''}
        onChange={(e) => onChange(fieldKey, e.target.value)}
        disabled={disabled}
        placeholder={fdef?.required ? 'Required' : 'Optional'}
        className={SELECT_CLASS}
      />
    </div>
  );
}

function ConfirmationDialog({ message, onConfirm, onCancel }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" role="dialog" aria-modal="true">
      <div className="bg-gray-800 rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl">
        <p className="text-gray-200 mb-6">{message}</p>
        <div className="flex justify-end gap-3">
          <button onClick={onCancel} className="px-4 py-2 text-sm rounded-lg bg-gray-700 hover:bg-gray-600 transition-colors">Cancel</button>
          <button onClick={onConfirm} className="px-4 py-2 text-sm rounded-lg bg-purple-600 hover:bg-purple-500 text-white transition-colors">Confirm</button>
        </div>
      </div>
    </div>
  );
}

const MODEL_SLOTS = [
  { key: 'storyteller_model', label: 'Storyteller Model', orProviderKey: 'openrouter_storyteller_provider', fallback: false },
  { key: 'storyteller_fallback_models', label: 'Fallback Models', orProviderKey: 'openrouter_fallback_provider', fallback: true },
  { key: 'reader_model', label: 'Reader Model', orProviderKey: 'openrouter_reader_provider', fallback: false },
  { key: 'embedding_model', label: 'Embedding Model', orProviderKey: 'openrouter_embedding_provider', fallback: false, embedding: true },
  { key: 'module_fast_model', label: 'Module Fast Model', orProviderKey: 'openrouter_fast_provider', fallback: false },
];

const LLM_KEYS = ['temperature', 'top_p', 'max_output_tokens', 'retry_attempts', 'retry_delay_seconds'];

export default function ModelSettings({ onBack, embedded = false }) {
  const [providers, setProviders] = useState([]);
  const [activeId, setActiveId] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [config, setConfig] = useState({});
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [models, setModels] = useState([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [showConfirmProvider, setShowConfirmProvider] = useState(null);
  const [keyLocked, setKeyLocked] = useState(false);
  const [keyValue, setKeyValue] = useState('');
  const [connecting, setConnecting] = useState(false);

  const loadProviders = useCallback(async () => {
    try {
      const data = await api.getProviders();
      setProviders(data.providers || []);
      const active = await api.getActiveProvider();
      setActiveId(active.active);
      const sid = active.active;
      setSelectedId(sid);
      setConfig(active.config || {});
      if (active.config?.api_key) {
        setKeyLocked(true);
        setKeyValue(active.config.api_key);
      }
    } catch (e) {
      console.error('Failed to load providers:', e);
    }
  }, []);

  useEffect(() => { loadProviders(); }, [loadProviders]);

  const fetchModelsFor = useCallback(async (pid) => {
    setLoadingModels(true);
    try {
      const result = await api.fetchProviderModels(pid);
      setModels(result.models || []);
    } catch (e) {
      console.error('Failed to fetch models:', e);
      setModels([]);
    } finally {
      setLoadingModels(false);
    }
  }, []);

  useEffect(() => {
    if (selectedId && keyLocked) {
      fetchModelsFor(selectedId);
    }
  }, [selectedId, keyLocked, fetchModelsFor]);

  const handleSelectProvider = useCallback(async (pid) => {
    setSelectedId(pid);
    setTestResult(null);
    setModels([]);
    setKeyLocked(false);
    setKeyValue('');
    try {
      const data = await api.getProviderConfig(pid);
      setConfig(data.config || {});
      if (data.config?.api_key) {
        setKeyLocked(true);
        setKeyValue(data.config.api_key);
      }
    } catch (e) {
      console.error('Failed to load provider config:', e);
    }
  }, []);

  const handleConnect = useCallback(async () => {
    if (!selectedId || !keyValue) return;
    setConnecting(true);
    setTestResult(null);
    const newConfig = { ...config, api_key: keyValue };
    try {
      await api.updateProviderConfig(selectedId, { api_key: keyValue });
      const result = await api.testProvider(selectedId);
      if (result.success) {
        setConfig(newConfig);
        setKeyLocked(true);
        setTestResult(result);
        await fetchModelsFor(selectedId);
      } else {
        setTestResult(result);
      }
    } catch (e) {
      setTestResult({ success: false, error: e.message });
    }
    setConnecting(false);
  }, [selectedId, keyValue, config, fetchModelsFor]);

  const handleClear = useCallback(async () => {
    if (!selectedId) return;
    try {
      await api.updateProviderConfig(selectedId, { api_key: '' });
    } catch (e) {
      console.error('Failed to clear key:', e);
    }
    setConfig(prev => ({ ...prev, api_key: '' }));
    setKeyLocked(false);
    setKeyValue('');
    setModels([]);
    setTestResult(null);
  }, [selectedId]);

  const handleSave = useCallback(async () => {
    if (!selectedId) return;
    setSaving(true);
    try {
      await api.updateProviderConfig(selectedId, config);
    } catch (e) {
      console.error('Failed to save config:', e);
    }
    setSaving(false);
  }, [selectedId, config]);

  const handleFieldChange = useCallback((key, value) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  }, []);

  const handleOrProviderChange = useCallback((orProviderKey, provider) => {
    setConfig((prev) => ({
      ...prev,
      [orProviderKey]: provider,
    }));
  }, []);

  const handleSetActive = useCallback((pid) => {
    setShowConfirmProvider(pid);
  }, []);

  const confirmSetActive = useCallback(async () => {
    const pid = showConfirmProvider;
    setShowConfirmProvider(null);
    try {
      const data = await api.setActiveProvider(pid);
      setActiveId(data.active);
      setConfig(data.config || {});
      if (data.config?.api_key) {
        setKeyLocked(true);
        setKeyValue(data.config.api_key);
      } else {
        setKeyLocked(false);
        setKeyValue('');
      }
    } catch (e) {
      console.error('Failed to set active provider:', e);
    }
  }, [showConfirmProvider]);

  const handleApplyPreset = useCallback(async (presetLabel) => {
    if (!selectedId) return;
    setSaving(true);
    try {
      const data = await api.applyProviderPreset(selectedId, presetLabel);
      setConfig(data.config || {});
      if (selectedId === activeId) {
        await api.setActiveProvider(selectedId);
      }
    } catch (e) {
      console.error('Failed to apply preset:', e);
    }
    setSaving(false);
  }, [selectedId, activeId]);

  const pdef = providers.find(p => p.id === selectedId) || {};
  const fields = pdef.fields || {};
  const presets = pdef.presets || [];
  const isOpenRouter = selectedId === 'openrouter';

  const orProviders = useMemo(() => {
    if (!isOpenRouter || models.length === 0) return [];
    const set = new Set();
    for (const m of models) {
      if (m.provider) set.add(m.provider);
    }
    return [...set].sort();
  }, [isOpenRouter, models]);

  const orEmbeddingProviders = useMemo(() => {
    if (!isOpenRouter || models.length === 0) return [];
    const set = new Set();
    for (const m of models) {
      if (m.is_embedding && m.provider) set.add(m.provider);
    }
    return [...set].sort();
  }, [isOpenRouter, models]);

  const getModelsForSlot = useCallback((slot) => {
    if (slot.embedding) {
      return models.filter(m => m.is_embedding);
    }
    return models;
  }, [models]);

  return (
    <div className={embedded ? '' : 'min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950'}>
      <div className={embedded ? '' : 'max-w-3xl mx-auto p-6'}>
        {!embedded && (
          <button onClick={onBack} className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-4">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            Back to Menu
          </button>
        )}

        {!embedded && <h2 className="text-2xl font-bold text-gray-100 mb-6">Model Settings</h2>}

        {/* Provider dropdown */}
        <div className="mb-6">
          <label className="text-sm text-gray-300 mb-2 block">Provider</label>
          <select
            value={selectedId}
            onChange={(e) => handleSelectProvider(e.target.value)}
            className={SELECT_CLASS}
          >
            <option value="" disabled>Select a provider...</option>
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}{p.active ? ' (active)' : ''}
              </option>
            ))}
          </select>
        </div>

        {selectedId && (
          <div className="space-y-6">
            {/* Active indicator */}
            {activeId === selectedId ? (
              <div className="px-4 py-2 bg-green-900/30 border border-green-700/50 rounded-lg text-green-400 text-sm">
                This is your active provider. All AI calls use these models.
              </div>
            ) : (
              <button
                onClick={() => handleSetActive(selectedId)}
                className="w-full px-4 py-2 bg-purple-700 hover:bg-purple-600 rounded-lg text-sm font-medium transition-colors"
              >
                Set as Active Provider
              </button>
            )}

            {/* Presets */}
            {presets.length > 0 && (
              <div className="p-4 bg-gray-800/60 rounded-lg">
                <h3 className="text-sm font-semibold text-gray-300 mb-3">Presets</h3>
                <div className="flex gap-2 flex-wrap">
                  {presets.map((preset) => (
                    <button
                      key={preset.label}
                      onClick={() => handleApplyPreset(preset.label)}
                      disabled={saving}
                      className="px-3 py-1.5 text-xs bg-gray-700 hover:bg-gray-600 rounded-lg text-gray-200 transition-colors disabled:opacity-50"
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* API Key */}
            {fields.api_key && (
              <div className="p-4 bg-gray-800/60 rounded-lg space-y-3">
                <h3 className="text-sm font-semibold text-gray-300">API Key</h3>
                <div className="flex gap-2">
                  <input
                    type="password"
                    value={keyLocked ? '••••••••' : keyValue}
                    onChange={(e) => setKeyValue(e.target.value)}
                    disabled={keyLocked}
                    placeholder={fields.api_key?.required ? 'Required' : 'Optional'}
                    className={`flex-1 ${SELECT_CLASS} ${keyLocked ? 'opacity-50 cursor-not-allowed' : ''}`}
                  />
                  {keyLocked ? (
                    <button
                      onClick={handleClear}
                      className="px-4 py-2 bg-red-700 hover:bg-red-600 rounded-lg text-sm text-white transition-colors"
                    >
                      Clear
                    </button>
                  ) : (
                    <button
                      onClick={handleConnect}
                      disabled={connecting || !keyValue}
                      className="px-4 py-2 bg-purple-600 hover:bg-purple-500 rounded-lg text-sm font-medium text-white transition-colors disabled:opacity-50"
                    >
                      {connecting ? 'Connecting...' : 'Connect'}
                    </button>
                  )}
                </div>
                {testResult && (
                  <div className={`p-3 rounded-lg text-sm ${testResult.success ? 'bg-green-900/30 border border-green-700/50 text-green-400' : 'bg-red-900/30 border border-red-700/50 text-red-400'}`}>
                    {testResult.success
                      ? `Connection successful with ${testResult.model}`
                      : `Connection failed: ${testResult.error}`}
                  </div>
                )}
              </div>
            )}

            {/* Models */}
            {keyLocked && (
              <div className="p-4 bg-gray-800/60 rounded-lg space-y-4">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-gray-300">Models</h3>
                  {loadingModels && (
                    <span className="text-xs text-gray-500">Fetching models...</span>
                  )}
                </div>
                {MODEL_SLOTS.map((slot) => {
                  const fdef = fields[slot.key];
                  if (!fdef) return null;
                  const slotModels = getModelsForSlot(slot);
                  const curVal = config[slot.key] || '';
                  if (slot.fallback) {
                    return (
                      <div key={slot.key}>
                        <label className="text-sm text-gray-300">{slot.label}</label>
                        {fdef.description && <p className="text-xs text-gray-500 mb-1">{fdef.description}</p>}
                        {isOpenRouter && (
                          <select
                            value={config[slot.orProviderKey] || ''}
                            onChange={(e) => handleOrProviderChange(slot.orProviderKey, e.target.value)}
                            className={`${SELECT_CLASS} mb-1`}
                          >
                            <option value="">Any provider (let OpenRouter decide)</option>
                            {orProviders.map((p) => (
                              <option key={p} value={p}>{p}</option>
                            ))}
                          </select>
                        )}
                        <input
                          type="text"
                          value={curVal}
                          onChange={(e) => handleFieldChange(slot.key, e.target.value)}
                          className={`${SELECT_CLASS} font-mono`}
                          placeholder="model1, model2, ..."
                        />
                      </div>
                    );
                  }
                  return (
                    <div key={slot.key}>
                      <label className="text-sm text-gray-300">{slot.label}</label>
                      {fdef.description && <p className="text-xs text-gray-500 mb-1">{fdef.description}</p>}
                      <div className="flex flex-col sm:flex-row gap-2">
                        <select
                          value={curVal}
                          onChange={(e) => handleFieldChange(slot.key, e.target.value)}
                          className={`sm:flex-1 min-w-0 ${SELECT_CLASS} font-mono`}
                        >
                          <option value="">Select a model...</option>
                          {slotModels.map((m) => (
                            <option key={m.id} value={m.id}>
                              {m.id}
                            </option>
                          ))}
                        </select>
                        {isOpenRouter && (
                          <select
                            value={config[slot.orProviderKey] || ''}
                            onChange={(e) => handleOrProviderChange(slot.orProviderKey, e.target.value)}
                            className={`sm:flex-1 min-w-0 ${SELECT_CLASS}`}
                          >
                            <option value="">Any provider (let OpenRouter decide)</option>
                            {(slot.embedding ? orEmbeddingProviders : orProviders).map((p) => (
                              <option key={p} value={p}>{p}</option>
                            ))}
                          </select>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {/* LLM Options */}
            <div className="p-4 bg-gray-800/60 rounded-lg space-y-4">
              <h3 className="text-sm font-semibold text-gray-300">LLM Options</h3>
              {LLM_KEYS.map((key) => {
                const fdef = fields[key];
                if (!fdef) return null;
                return (
                  <FieldInput
                    key={key}
                    fieldKey={key}
                    fdef={fdef}
                    value={config[key]}
                    onChange={handleFieldChange}
                    disabled={false}
                  />
                );
              })}
            </div>

            {/* Actions */}
            <div className="flex gap-3">
              <button
                onClick={handleSave}
                disabled={saving}
                className={`flex-1 ${BTN_PRIMARY}`}
              >
                {saving ? 'Saving...' : 'Save Changes'}
              </button>
            </div>
          </div>
        )}
      </div>

      {showConfirmProvider && (
        <ConfirmationDialog
          message={`Switch active provider to ${providers.find(p => p.id === showConfirmProvider)?.label || showConfirmProvider}? All AI calls will use the new provider's models.`}
          onConfirm={confirmSetActive}
          onCancel={() => setShowConfirmProvider(null)}
        />
      )}
    </div>
  );
}
