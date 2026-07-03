import { useState, useEffect } from 'react';
import { api } from 'api';

// Story-source picker contributed by wb_worldgen, embedded in the storyteller
// start screen. Controlled component: the host owns the chosen story source
// (`selected`) and updates it via `onSelect`. Reporting a world clears any other
// source (the host enforces single-selection). Renders nothing if there are no
// saved worlds yet.
export default function StorytellerStart({ selected, onSelect }) {
  const [worlds, setWorlds] = useState([]);
  const [startPreference, setStartPreference] = useState('');
  const [startLocation, setStartLocation] = useState(null);
  const [picking, setPicking] = useState(false);

  useEffect(() => {
    api.listWorlds().then((d) => setWorlds(d.worlds || [])).catch(() => {});
  }, []);

  const activeWorldId = selected && selected.type === 'world' ? selected.id : null;

  // If the host switched to another source (e.g. a scenario), drop local UI.
  useEffect(() => {
    if (!activeWorldId) {
      setStartPreference('');
      setStartLocation(null);
    }
  }, [activeWorldId]);

  const chooseWorld = (worldId) => {
    setStartPreference('');
    setStartLocation(null);
    onSelect({ type: 'world', id: worldId, startPreference: '', startLocation: null });
  };

  const updatePreference = (value) => {
    setStartPreference(value);
    if (activeWorldId) {
      onSelect({ type: 'world', id: activeWorldId, startPreference: value, startLocation });
    }
  };

  const handlePickStart = async () => {
    if (!activeWorldId || !startPreference.trim()) return;
    setPicking(true);
    try {
      const result = await api.pickStartLocation(activeWorldId, startPreference.trim());
      setStartLocation(result.location);
      onSelect({ type: 'world', id: activeWorldId, startPreference, startLocation: result.location });
    } catch (e) {
      alert(`Failed to pick start location: ${e.message}`);
    }
    setPicking(false);
  };

  if (worlds.length === 0) return null;

  return (
    <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30">
      <h4 className="text-sm font-medium text-gray-300 mb-2">Select a World (optional)</h4>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-3">
        <button
          onClick={() => onSelect(null)}
          className={`p-3 rounded-lg border text-sm text-left transition-colors ${
            !activeWorldId
              ? 'border-purple-500 bg-purple-900/30 text-purple-200'
              : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
          }`}
        >
          <span className="font-medium">No World</span>
          <p className="text-xs text-gray-500 mt-0.5">Blank canvas</p>
        </button>
        {worlds.map((w) => (
          <button
            key={w.id}
            onClick={() => chooseWorld(w.id)}
            className={`p-3 rounded-lg border text-sm text-left transition-colors ${
              activeWorldId === w.id
                ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
            }`}
          >
            <span className="font-medium">{w.name}</span>
            <p className="text-xs text-gray-500 mt-0.5 truncate">{(w.seed_prompt || '').substring(0, 60)}</p>
          </button>
        ))}
      </div>

      {activeWorldId && (
        <div className="space-y-2 pt-2 border-t border-gray-700">
          <p className="text-xs text-gray-400">Starting location preference (optional)</p>
          <div className="flex gap-2">
            <input
              value={startPreference}
              onChange={(e) => updatePreference(e.target.value)}
              placeholder="e.g., coastal trading city"
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
            />
            <button
              onClick={handlePickStart}
              disabled={!startPreference.trim() || picking}
              className="px-3 py-1.5 rounded-lg bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-xs font-medium transition-colors"
            >
              {picking ? 'Picking...' : 'Pick for me'}
            </button>
          </div>
          {startLocation && (
            <div className="p-2 rounded bg-purple-900/20 border border-purple-800/30 text-sm">
              <p className="text-purple-300 font-medium">{startLocation.name}</p>
              <p className="text-gray-400 text-xs mt-0.5">{startLocation.description?.substring(0, 150)}</p>
              {startLocation.reason && (
                <p className="text-purple-400/70 text-xs mt-1 italic">"{startLocation.reason}"</p>
              )}
            </div>
          )}
          {!startPreference.trim() && !startLocation && (
            <p className="text-xs text-gray-500 italic">Leave empty for a random start location.</p>
          )}
        </div>
      )}
    </div>
  );
}
