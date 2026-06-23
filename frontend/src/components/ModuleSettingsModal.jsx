import { useState, useEffect } from 'react';
import React from 'react';
import { api } from '../lib/api';

let babelPromise = null;

function ensureBabel() {
  if (!babelPromise) {
    babelPromise = import('@babel/standalone').then(m => m.default || m);
  }
  return babelPromise;
}

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

    api.getWidgetFile(mod.id, 'widget_settings.jsx').then(async res => {
      if (!res.ok) {
        setError('This module has no advanced settings.');
        setLoading(false);
        return;
      }
      try {
        const source = await res.text();
        const Babel = await ensureBabel();
        const compiled = Babel.transform(source, {
          presets: [
            ['env', { modules: 'commonjs' }],
            ['react', { runtime: 'classic' }]
          ]
        }).code;

        const factory = new Function('require', 'module', 'exports', 'React', compiled);
        const m = { exports: {} };
        const requireMock = (name) => {
          if (name === 'react') return React;
          throw new Error(`Module ${name} not found`);
        };
        factory(requireMock, m, m.exports, React);
        const Component = m.exports?.default || m.exports;
        CACHE[cacheKey] = Component;
        setWidget(() => Component);
      } catch (e) {
        setError(`Failed to compile settings widget: ${e.message}`);
      }
      setLoading(false);
    }).catch(e => {
      setError(e.message || 'Failed to load settings widget');
      setLoading(false);
    });
  }, [isOpen, mod]);

  if (!isOpen || !mod) return null;

  const config = moduleConfigs?.[mod.id] || {};

  const handleSaveConfig = (updatedModuleConfig) => {
    const merged = { ...moduleConfigs, [mod.id]: { ...config, ...updatedModuleConfig } };
    onSaveModuleConfigs(merged);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" role="dialog" aria-modal="true">
      <div className="bg-gray-800 w-full max-w-xl rounded-lg shadow-2xl border border-gray-700 flex flex-col max-h-[85vh]">
        <div className="p-4 border-b border-gray-700 flex justify-between items-center bg-gray-900 rounded-t-lg">
          <h2 className="text-lg font-bold text-gray-100">{mod?.name} — Settings</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-2xl leading-none" aria-label="Close">&times;</button>
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
