import { useEffect, useState } from 'react';
import { api } from '../../lib/api';

// Renders global engine settings in the "Storytelling" category (default
// narrative perspective and tense). Values save immediately on change.
export default function StorytellingSettings() {
  const [descriptors, setDescriptors] = useState(null);
  const [values, setValues] = useState({});
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    api.getSettings('global')
      .then((data) => {
        if (cancelled) return;
        const items = (data?.settings?.Storytelling) || [];
        setDescriptors(items);
        setValues(Object.fromEntries(items.map((d) => [d.key, d.value ?? d.default])));
      })
      .catch((e) => { if (!cancelled) setError(e.message || 'Failed to load storytelling settings.'); });
    return () => { cancelled = true; };
  }, []);

  async function setValue(key, value) {
    const prev = values[key];
    setValues((v) => ({ ...v, [key]: value }));
    setError('');
    try {
      await api.updateSettings({ [key]: value }, 'global');
    } catch (e) {
      setValues((v) => ({ ...v, [key]: prev }));
      setError(e.message || 'Failed to save.');
    }
  }

  if (error && !descriptors) return <div className="text-sm text-red-400">{error}</div>;
  if (!descriptors) return <div className="text-sm text-gray-500">Loading…</div>;
  if (descriptors.length === 0) return <div className="text-sm text-gray-500">No storytelling settings available.</div>;

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-4 space-y-4">
      {descriptors.map((d) => (
        <div key={d.key} className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 sm:gap-4">
          <div className="flex flex-col">
            <span className="text-sm font-medium text-gray-300">{d.label}</span>
            {d.description && <span className="text-xs text-gray-500">{d.description}</span>}
          </div>
          {d.type === 'select' && (
            <select
              value={values[d.key] ?? d.default}
              onChange={(e) => setValue(d.key, e.target.value)}
              aria-label={d.label}
              className="shrink-0 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-purple-500"
            >
              {(d.options || []).map((opt) => {
                const v = typeof opt === 'object' ? opt.value : opt;
                const l = typeof opt === 'object' ? (opt.label || v) : v;
                return <option key={v} value={v}>{l}</option>;
              })}
            </select>
          )}
        </div>
      ))}
      {error && <div className="text-xs text-red-400">{error}</div>}
    </div>
  );
}
