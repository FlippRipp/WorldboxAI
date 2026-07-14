import { useEffect, useState } from 'react';
import { api } from '../../lib/api';

// Renders every global engine setting in the "Cheats" category (currently the
// master cheat toggle). Values save immediately on change — cheats shouldn't
// need a save button.
export default function CheatSettings() {
  const [descriptors, setDescriptors] = useState(null);
  const [values, setValues] = useState({});
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    api.getSettings('global')
      .then((data) => {
        if (cancelled) return;
        const cheats = (data?.settings?.Cheats) || [];
        setDescriptors(cheats);
        setValues(Object.fromEntries(cheats.map((d) => [d.key, d.value ?? d.default])));
      })
      .catch((e) => { if (!cancelled) setError(e.message || 'Failed to load cheat settings.'); });
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
  if (descriptors.length === 0) return <div className="text-sm text-gray-500">No cheat settings available.</div>;

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-4 space-y-4">
      {descriptors.map((d) => (
        <div key={d.key} className="flex items-center justify-between gap-4">
          <div className="flex flex-col">
            <span className="text-sm font-medium text-gray-300">{d.label}</span>
            {d.description && <span className="text-xs text-gray-500">{d.description}</span>}
          </div>
          {d.type === 'toggle' && (
            <label className="relative inline-flex items-center cursor-pointer flex-shrink-0">
              <input
                type="checkbox"
                checked={!!values[d.key]}
                onChange={(e) => setValue(d.key, e.target.checked)}
                className="sr-only peer"
                aria-label={d.label}
              />
              <div className="w-11 h-6 bg-gray-600 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-purple-600" />
            </label>
          )}
        </div>
      ))}
      {error && <div className="text-xs text-red-400">{error}</div>}
    </div>
  );
}
