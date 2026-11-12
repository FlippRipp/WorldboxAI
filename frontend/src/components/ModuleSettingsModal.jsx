import { useState, useEffect } from 'react';
import { loadModuleComponent } from './shared/moduleLoader';

const CACHE = {};

export default function ModuleSettingsModal({ isOpen, onClose, mod, gameState, moduleConfigs, onSaveModuleConfigs }) {
  const [Widget, setWidget] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!isOpen || !mod) return;
    setLoading(true);
    setError(null);

    const cacheKey = `${mod.id}_settings`;
    if (CACHE[cacheKey]) {
      setWidget(() => CACHE[cacheKey]);
      setLoading(false);
      return;
    }

    let cancelled = false;
    loadModuleComponent(mod.id, 'widget_settings.jsx')
      .then(Component => {
        if (cancelled) return;
        CACHE[cacheKey] = Component;
        setWidget(() => Component);
        setLoading(false);
      })
      .catch(e => {
        if (cancelled) return;
        if (e?.status === 404 || String(e?.message || '').includes('404')) {
          setError('This module has no advanced settings.');
        } else {
          setError(`Failed to compile settings widget: ${e.message || 'Unknown error'}`);
        }
        setLoading(false);
      });

    return () => { cancelled = true; };
  }, [isOpen, mod]);

  if (!isOpen || !mod) return null;

  const config = moduleConfigs?.[mod.id] || {};

  const handleSaveConfig = (updatedModuleConfig) => {
    const merged = { ...moduleConfigs, [mod.id]: { ...config, ...updatedModuleConfig } };
    onSaveModuleConfigs(merged);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-3 sm:p-4" role="dialog" aria-modal="true">
      <div className="bg-gray-800 w-full max-w-xl rounded-lg shadow-2xl border border-gray-700 flex flex-col max-h-[88dvh]">
        <div className="px-4 py-3 border-b border-gray-700 flex justify-between items-center bg-gray-900 rounded-t-lg gap-2">
          <h2 className="text-lg font-bold text-gray-100 truncate">{mod?.name} — Settings</h2>
          <button onClick={onClose} className="min-h-11 min-w-11 flex items-center justify-center text-gray-400 hover:text-white text-2xl leading-none shrink-0" aria-label="Close">&times;</button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="p-8 text-center text-gray-400 animate-pulse">Loading settings...</div>
          )}
          {error && (
            <div className="p-4 text-center text-gray-500">{error}</div>
          )}
          {Widget && !loading && (
            <Widget config={config} onSaveConfig={handleSaveConfig} />
          )}
        </div>
      </div>
    </div>
  );
}
