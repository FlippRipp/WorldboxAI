import { useState, useEffect } from 'react';
import { api } from 'api';

// Story-source picker contributed by wb_worldgen, embedded in the storyteller
// start screen. Controlled component: the host owns the chosen story source
// (`selected`) and updates it via `onSelect`. The host may pair the world with
// an independently chosen scenario (world = setting, scenario = opening);
// `scenarioSelected` reflects that pairing — the start location then follows
// from the scenario's opening, so the preference UI is hidden.
// Renders nothing if there are no saved worlds yet.
export default function StorytellerStart({ selected, onSelect, scenarioSelected = false }) {
  const [worlds, setWorlds] = useState([]);
  const [worldsError, setWorldsError] = useState(false);
  // Hydrate from the host's selection so remounting (e.g. leaving and
  // re-entering the create form) keeps showing the already-picked start.
  const [startPreference, setStartPreference] = useState(() => selected?.startPreference || '');
  const [startLocation, setStartLocation] = useState(() => selected?.startLocation || null);
  const [picking, setPicking] = useState(false);
  const [pickError, setPickError] = useState(null);

  const loadWorlds = () => {
    setWorldsError(false);
    api.listWorlds()
      .then((d) => setWorlds(d.worlds || []))
      .catch(() => setWorldsError(true));
  };
  useEffect(loadWorlds, []);

  const activeWorldId = selected && selected.type === 'world' ? selected.id : null;

  // If the host switched to another source (e.g. a scenario), drop local UI.
  useEffect(() => {
    if (!activeWorldId) {
      setStartPreference('');
      setStartLocation(null);
    }
  }, [activeWorldId]);

  // A world created with a linked scenario carries its scenario_id; surfacing
  // it on the selection lets the host pre-select that scenario.
  const linkedScenarioId = (worldId) =>
    worlds.find((w) => w.id === worldId)?.scenario_id || null;

  const chooseWorld = (worldId) => {
    setStartPreference('');
    setStartLocation(null);
    setPickError(null);
    onSelect({ type: 'world', id: worldId, startPreference: '', startLocation: null, linkedScenarioId: linkedScenarioId(worldId) });
  };

  const updatePreference = (value) => {
    setStartPreference(value);
    if (activeWorldId) {
      onSelect({ type: 'world', id: activeWorldId, startPreference: value, startLocation, linkedScenarioId: linkedScenarioId(activeWorldId) });
    }
  };

  const handlePickStart = async () => {
    if (!activeWorldId || !startPreference.trim() || picking) return;
    setPicking(true);
    setPickError(null);
    try {
      const result = await api.pickStartLocation(activeWorldId, startPreference.trim());
      setStartLocation(result.location);
      onSelect({ type: 'world', id: activeWorldId, startPreference, startLocation: result.location, linkedScenarioId: linkedScenarioId(activeWorldId) });
    } catch (e) {
      setPickError(e.message);
    }
    setPicking(false);
  };

  if (worldsError) {
    return (
      <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30 flex items-center justify-between gap-3">
        <p className="text-sm text-red-400">Couldn't load worlds.</p>
        <button
          onClick={loadWorlds}
          className="px-3 py-1.5 rounded-lg border border-gray-700 hover:bg-gray-800 text-xs text-gray-300 transition-colors"
        >
          Retry
        </button>
      </div>
    );
  }
  if (worlds.length === 0) return null;

  return (
    <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30">
      <h4 className="text-sm font-medium text-gray-300 mb-2">Select a World (optional)</h4>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-3">
        <button
          onClick={() => onSelect(null)}
          aria-pressed={!activeWorldId}
          className={`p-3 rounded-lg border text-sm text-left transition-colors ${
            !activeWorldId
              ? 'border-purple-500 bg-purple-900/30 text-purple-200'
              : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
          }`}
        >
          <span className="font-medium">{!activeWorldId && '✓ '}No World</span>
          <p className="text-xs text-gray-500 mt-0.5">Blank canvas</p>
        </button>
        {worlds.map((w) => (
          <button
            key={w.id}
            onClick={() => chooseWorld(w.id)}
            aria-pressed={activeWorldId === w.id}
            className={`p-3 rounded-lg border text-sm text-left transition-colors ${
              activeWorldId === w.id
                ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
            }`}
          >
            <span className="font-medium">{activeWorldId === w.id && '✓ '}{w.name}</span>
            <p className="text-xs text-gray-500 mt-0.5 truncate">{(w.seed_prompt || '').substring(0, 60)}</p>
          </button>
        ))}
      </div>

      {activeWorldId && scenarioSelected && (
        <div className="pt-2 border-t border-gray-700">
          <p className="text-xs text-gray-500 italic">
            The starting location will be chosen to fit the selected scenario's opening.
          </p>
        </div>
      )}
      {activeWorldId && !scenarioSelected && (
        <div className="space-y-2 pt-2 border-t border-gray-700">
          <p className="text-xs text-gray-400">Starting location preference (optional)</p>
          <div className="flex gap-2">
            <input
              value={startPreference}
              onChange={(e) => updatePreference(e.target.value)}
              placeholder="e.g., coastal trading city"
              aria-label="Starting location preference"
              className="flex-1 min-h-[44px] bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
            />
            <button
              onClick={handlePickStart}
              disabled={!startPreference.trim() || picking}
              aria-busy={picking}
              aria-label="Pick a start location matching my preference"
              className="px-3 py-1.5 min-h-[44px] rounded-lg bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-xs font-medium transition-colors"
            >
              {picking ? 'Picking...' : 'Pick for me'}
            </button>
          </div>
          {pickError && (
            <p className="text-xs text-red-400">Failed to pick start location: {pickError}</p>
          )}
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
