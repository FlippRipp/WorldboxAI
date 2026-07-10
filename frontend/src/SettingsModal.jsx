import React, { useState, useEffect } from 'react';
import { api } from './lib/api';
import ModuleSettingsModal from './components/ModuleSettingsModal';

const PROVIDER_PRESETS = {
  gemini: {
    label: 'Google Gemini',
    models: {
      'llm.storyteller_model': 'gemini/gemini-2.5-flash',
      'llm.reader_model': 'gemini/gemini-2.5-flash',
      'llm.embedding_model': 'gemini/gemini-embedding-2',
      'llm.module_fast_model': 'gemini/gemini-2.5-flash',
    }
  },
  openrouter: {
    label: 'OpenRouter',
    models: {
      'llm.storyteller_model': 'openrouter/anthropic/claude-sonnet-4-20250514',
      'llm.reader_model': 'openrouter/meta-llama/llama-4-maverick',
      'llm.embedding_model': 'openrouter/google/gemini-embedding-2',
      'llm.module_fast_model': 'openrouter/meta-llama/llama-4-maverick',
    }
  },
  openai: {
    label: 'OpenAI',
    models: {
      'llm.storyteller_model': 'openai/gpt-4o',
      'llm.reader_model': 'openai/gpt-4o-mini',
      'llm.embedding_model': 'openai/text-embedding-3-small',
      'llm.module_fast_model': 'openai/gpt-4o-mini',
    }
  },
  anthropic: {
    label: 'Anthropic Claude',
    models: {
      'llm.storyteller_model': 'anthropic/claude-sonnet-4-20250514',
      'llm.reader_model': 'anthropic/claude-haiku',
      'llm.embedding_model': '',
      'llm.module_fast_model': 'anthropic/claude-haiku',
    }
  },
};

const TYPE_COMPONENTS = {
  slider: ({ descriptor, value, onChange }) => (
    <div className="flex flex-col gap-1">
      <label className="text-sm font-medium text-gray-300 flex justify-between">
        <span>{descriptor.label}</span>
        <span className="text-purple-400 font-mono">{value}</span>
      </label>
      <input
        type="range"
        min={descriptor.min}
        max={descriptor.max}
        value={value}
        onChange={(e) => onChange(parseInt(e.target.value))}
        className="w-full accent-purple-500"
        aria-label={descriptor.label}
      />
      {descriptor.description && (
        <p className="text-xs text-gray-500">{descriptor.description}</p>
      )}
    </div>
  ),
  toggle: ({ descriptor, value, onChange }) => (
    <div className="flex items-center justify-between">
      <div className="flex flex-col">
        <span className="text-sm font-medium text-gray-300">{descriptor.label}</span>
        {descriptor.description && (
          <span className="text-xs text-gray-500">{descriptor.description}</span>
        )}
      </div>
      <label className="relative inline-flex items-center cursor-pointer flex-shrink-0">
        <input
          type="checkbox"
          checked={value}
          onChange={(e) => onChange(e.target.checked)}
          className="sr-only peer"
          aria-label={descriptor.label}
        />
        <div className="w-11 h-6 bg-gray-600 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-purple-600" />
      </label>
    </div>
  ),
  select: ({ descriptor, value, onChange }) => (
    <div className="flex flex-col gap-1">
      <label className="text-sm font-medium text-gray-300">{descriptor.label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-gray-700 border border-gray-600 rounded px-3 py-1.5 text-gray-200 text-sm"
        aria-label={descriptor.label}
      >
        {(descriptor.options || []).map((opt) => {
          const v = typeof opt === 'object' ? opt.value : opt;
          const l = typeof opt === 'object' ? (opt.label || v) : v;
          return <option key={v} value={v}>{l}</option>;
        })}
      </select>
      {descriptor.description && (
        <p className="text-xs text-gray-500">{descriptor.description}</p>
      )}
    </div>
  ),
  text: ({ descriptor, value, onChange }) => (
    <div className="flex flex-col gap-1">
      <label className="text-sm font-medium text-gray-300">{descriptor.label}</label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-gray-700 border border-gray-600 rounded px-3 py-1.5 text-gray-200 text-sm"
        aria-label={descriptor.label}
      />
      {descriptor.description && (
        <p className="text-xs text-gray-500">{descriptor.description}</p>
      )}
    </div>
  ),
  secret: ({ descriptor, value, onChange }) => (
    <div className="flex flex-col gap-1">
      <label className="text-sm font-medium text-gray-300">{descriptor.label}</label>
      <input
        type="password"
        value={value || ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder={value === '••••••••' ? '••••••••' : 'Enter key...'}
        className="bg-gray-700 border border-gray-600 rounded px-3 py-1.5 text-gray-200 text-sm font-mono"
        aria-label={descriptor.label}
      />
      {descriptor.description && (
        <p className="text-xs text-gray-500">{descriptor.description}</p>
      )}
    </div>
  ),
};

function SettingControl({ descriptor, value, onChange }) {
  const Renderer = TYPE_COMPONENTS[descriptor.type];
  if (!Renderer) {
    return <div className="text-red-400 text-xs">Unknown type: {descriptor.type}</div>;
  }
  return <Renderer descriptor={descriptor} value={value} onChange={onChange} />;
}

export default function SettingsModal({ isOpen, onClose, modules, moduleConfigs, onSaveModuleConfigs, gameState, scope = 'story' }) {
  const [engineSettings, setEngineSettings] = useState({});
  const [engineValues, setEngineValues] = useState({});
  const [moduleValues, setModuleValues] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [advSettingsMod, setAdvSettingsMod] = useState(null);

  const isGlobal = scope === 'global';

  useEffect(() => {
    if (!isOpen) return;
    setLoading(true);
    setError(null);

    api.getSettings(scope).then(data => {
      setEngineSettings(data.settings || {});
      const values = {};
      Object.values(data.settings || {}).forEach(items => {
        items.forEach(s => { values[s.key] = s.value; });
      });
      setEngineValues(values);
      setLoading(false);
    }).catch(err => {
      console.error('Failed to load engine settings:', err);
      setError(err.message || 'Failed to load engine settings');
      setLoading(false);
    });

    if (isGlobal) return;

    const initialModule = {};
    modules.forEach(mod => {
      if (mod.settings_schema) {
        initialModule[mod.id] = {};
        Object.entries(mod.settings_schema).forEach(([key, schema]) => {
          initialModule[mod.id][key] = moduleConfigs?.[mod.id]?.[key] ?? schema.default;
        });
      }
    });
    setModuleValues(initialModule);
  }, [isOpen, scope, modules, moduleConfigs]);

  if (!isOpen) return null;

  const handleEngineChange = (key, value) => {
    setEngineValues(prev => ({ ...prev, [key]: value }));
  };

  const handleModuleChange = (modId, key, value) => {
    setModuleValues(prev => ({
      ...prev,
      [modId]: { ...prev[modId], [key]: value }
    }));
  };

  const handleSave = async () => {
    const engineUpdates = {};
    Object.values(engineSettings).forEach(items => {
      items.forEach(s => {
        if (engineValues[s.key] !== s.value) {
          engineUpdates[s.key] = engineValues[s.key];
        }
      });
    });
    if (Object.keys(engineUpdates).length > 0) {
      await api.updateSettings(engineUpdates, scope);
    }
    if (!isGlobal) {
      await onSaveModuleConfigs(moduleValues);
    }
    onClose();
  };

  const handleProviderChange = (newProvider) => {
    const preset = PROVIDER_PRESETS[newProvider];
    if (!preset) {
      handleEngineChange('llm.provider', newProvider);
      return;
    }
    const updates = { 'llm.provider': newProvider, ...preset.models };
    setEngineValues(prev => ({ ...prev, ...updates }));
  };

  const engineCategories = Object.entries(engineSettings);
  const hasEngineSettings = engineCategories.some(([, items]) => items.length > 0);
  const hasModuleSettings = modules.some(mod => Object.keys(mod.settings_schema || {}).length > 0);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 sm:p-4" role="dialog" aria-modal="true">
      {/* Full-height sheet on phones, centered card on larger screens. */}
      <div className="bg-gray-800 w-full max-w-2xl sm:rounded-lg shadow-2xl border border-gray-700 flex flex-col h-full sm:h-auto sm:max-h-[85vh]">
        <div className="p-4 border-b border-gray-700 flex justify-between items-center bg-gray-900 sm:rounded-t-lg">
          <h2 className="text-xl font-bold text-gray-100">{isGlobal ? 'Model Settings' : 'Settings'}</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-2xl leading-none" aria-label="Close settings">&times;</button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-6">
          {loading && (
            <div className="text-center text-gray-400 italic py-8 animate-pulse">Loading settings...</div>
          )}

          {!loading && error && (
            <div className="text-center text-red-400 italic py-4 bg-red-900/30 rounded border border-red-800">
              {error}
            </div>
          )}

          {!loading && !error && hasEngineSettings && (
            <div>
              <h3 className="text-sm uppercase tracking-wider text-gray-500 mb-4 font-semibold">Engine Settings</h3>
              <div className="space-y-6">
                {engineCategories.map(([category, items]) => (
                  <div key={category} className="bg-gray-900/50 p-4 rounded border border-gray-700">
                    <h4 className="text-lg font-semibold text-purple-400 mb-4">{category}</h4>
                    <div className="space-y-4">
                      {items.map(descriptor => (
                        <SettingControl
                          key={descriptor.key}
                          descriptor={descriptor}
                          value={engineValues[descriptor.key] ?? descriptor.default}
                          onChange={(v) => handleEngineChange(descriptor.key, v)}
                        />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {hasEngineSettings && hasModuleSettings && <hr className="border-gray-700" />}

          {hasModuleSettings && (
            <div>
              <h3 className="text-sm uppercase tracking-wider text-gray-500 mb-4 font-semibold">Module Settings</h3>
              <div className="space-y-6">
                {modules.map(mod => {
                  const schemaKeys = Object.keys(mod.settings_schema || {});
                  if (schemaKeys.length === 0) return null;
                  return (
                    <div key={mod.id} className="bg-gray-900/50 p-4 rounded border border-gray-700">
                      <h4 className="text-lg font-semibold text-green-400 mb-4">{mod.name}</h4>
                      <div className="space-y-4">
                        {Object.entries(mod.settings_schema).map(([key, schema]) => (
                          <SettingControl
                            key={key}
                            descriptor={{
                              key: `${mod.id}.${key}`,
                              type: schema.type,
                              label: schema.label || key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
                              min: schema.min,
                              max: schema.max,
                              default: schema.default,
                              options: schema.options,
                              description: schema.description,
                            }}
                            value={moduleValues[mod.id]?.[key] ?? schema.default}
                            onChange={(v) => handleModuleChange(mod.id, key, v)}
                          />
                        ))}
                      </div>
                      <button
                        onClick={() => setAdvSettingsMod(mod)}
                        className="mt-3 text-xs text-purple-400 hover:text-purple-300 hover:underline"
                      >
                        Configure Advanced Settings
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {!loading && !error && !hasEngineSettings && !hasModuleSettings && (
            <div className="text-center text-gray-500 italic py-8">No configurable settings available.</div>
          )}
        </div>

        <div className="p-4 border-t border-gray-700 bg-gray-900 sm:rounded-b-lg flex justify-end">
          <button
            onClick={handleSave}
            className="px-4 py-2 bg-purple-600 hover:bg-purple-500 text-white rounded font-medium transition-colors"
          >
            Save & Close
          </button>
        </div>
      </div>

      <ModuleSettingsModal
        isOpen={!!advSettingsMod}
        onClose={() => setAdvSettingsMod(null)}
        mod={advSettingsMod}
        gameState={gameState}
        moduleConfigs={moduleConfigs}
        onSaveModuleConfigs={onSaveModuleConfigs}
      />
    </div>
  );
}
