import { useState, useEffect } from 'react';
import { api } from '../../lib/api';

export default function SaveSelectScreen({ onLoad, onCreate, onBack }) {
  const [saves, setSaves] = useState([]);
  const [worlds, setWorlds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [characters, setCharacters] = useState([]);
  const [selectedCharacter, setSelectedCharacter] = useState(null);
  const [newName, setNewName] = useState('');
  const [selectedWorld, setSelectedWorld] = useState(null);
  const [startPreference, setStartPreference] = useState('');
  const [pickingStart, setPickingStart] = useState(false);
  const [startLocation, setStartLocation] = useState(null);
  const [creating, setCreating] = useState(false);
  const [loadingSave, setLoadingSave] = useState(null);

  useEffect(() => {
    Promise.all([
      api.getSaves(),
      api.listWorlds().catch(() => ({ worlds: [] })),
      api.listCharacters().catch(() => ({ characters: [] })),
    ])
      .then(([savesData, worldsData, charsData]) => {
        setSaves(savesData.saves || []);
        setWorlds(worldsData.worlds || []);
        setCharacters(charsData.characters || []);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const handleLoad = async (saveId) => {
    setLoadingSave(saveId);
    try {
      await api.loadSave(saveId);
      onLoad(saveId);
    } catch (e) {
      alert(`Failed to load: ${e.message}`);
    }
    setLoadingSave(null);
  };

  const handlePickStart = async () => {
    if (!selectedWorld || !startPreference.trim()) return;
    setPickingStart(true);
    try {
      const result = await api.pickStartLocation(selectedWorld.id, startPreference.trim());
      setStartLocation(result.location);
    } catch (e) {
      alert(`Failed to pick start location: ${e.message}`);
    }
    setPickingStart(false);
  };

  const handleDelete = async (saveId) => {
    if (!window.confirm(`Delete "${saveId}"? This cannot be undone.`)) return;
    try {
      await api.deleteSave(saveId);
      setSaves(prev => prev.filter(s => s.id !== saveId));
    } catch (e) {
      alert(`Failed to delete: ${e.message}`);
    }
  };

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) return;
    setCreating(true);
    try {
      const worldId = selectedWorld ? selectedWorld.id : null;
      const pref = startLocation ? null : (startPreference.trim() || null);
      const charId = selectedCharacter ? selectedCharacter.id : null;
      await api.createSave(name, worldId, pref, charId);
      onLoad(name);
    } catch (e) {
      alert(`Failed to create save: ${e.message}`);
      setCreating(false);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center p-6">
      <div className="w-full max-w-2xl">
        <button
          onClick={onBack}
          className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-8"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Menu
        </button>

        <h2 className="text-3xl font-bold text-gray-100 mb-2">Load a Story</h2>
        <p className="text-gray-500 text-sm mb-8">Pick an existing save or create a new one.</p>

        {loading ? (
          <div className="text-gray-500 text-center py-12">Loading saves...</div>
        ) : (
          <>
            {saves.length > 0 && (
              <div className="space-y-2 mb-8">
                {saves.map(save => (
                  <div
                    key={save.id}
                    className="flex items-center justify-between p-4 rounded-lg border border-gray-700 bg-gray-800/50 hover:bg-gray-800 transition-colors"
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-xl">📁</span>
                      <div>
                        <h4 className="font-medium text-gray-200">{save.id}</h4>
                        <p className="text-xs text-gray-500">
                          {save.active ? 'Active session' : 'Available'}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleLoad(save.id)}
                        disabled={loadingSave === save.id}
                        className="px-4 py-1.5 rounded-lg bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-sm transition-colors"
                      >
                        {loadingSave === save.id ? 'Loading...' : 'Load'}
                      </button>
                      <button
                        onClick={() => handleDelete(save.id)}
                        disabled={loadingSave === save.id}
                        className="px-3 py-1.5 rounded-lg bg-red-900/50 hover:bg-red-800 border border-red-800/50 hover:border-red-700 disabled:opacity-50 text-sm text-red-300 transition-colors"
                        title="Delete save"
                      >
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {saves.length === 0 && (
              <p className="text-gray-500 text-center mb-8 py-8 border border-dashed border-gray-700 rounded-lg">
                No saves yet. Create one below.
              </p>
            )}

            <div className="border-t border-gray-700 pt-6">
              <h3 className="text-lg font-semibold text-gray-200 mb-3">Create New Story</h3>
              <div className="space-y-4">
                <div className="flex gap-2">
                  <input
                    value={newName}
                    onChange={e => setNewName(e.target.value)}
                    placeholder="my_story"
                    onKeyDown={e => { if (e.key === 'Enter') handleCreate(); }}
                    className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                    aria-label="New save name"
                  />
                  <button
                    onClick={handleCreate}
                    disabled={!newName.trim() || creating}
                    className="px-6 py-2 rounded-lg bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-sm font-medium transition-colors"
                  >
                    {creating ? 'Creating...' : 'Create'}
                  </button>
                </div>

                {worlds.length > 0 && (
                  <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30">
                    <h4 className="text-sm font-medium text-gray-300 mb-2">Select a World (optional)</h4>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-3">
                      <button
                        onClick={() => { setSelectedWorld(null); setStartLocation(null); setStartPreference(''); }}
                        className={`p-3 rounded-lg border text-sm text-left transition-colors ${
                          !selectedWorld
                            ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                            : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                        }`}
                      >
                        <span className="font-medium">No World</span>
                        <p className="text-xs text-gray-500 mt-0.5">Blank canvas</p>
                      </button>
                      {worlds.map(w => (
                        <button
                          key={w.id}
                          onClick={() => { setSelectedWorld(w); setStartLocation(null); }}
                          className={`p-3 rounded-lg border text-sm text-left transition-colors ${
                            selectedWorld?.id === w.id
                              ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                              : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                          }`}
                        >
                          <span className="font-medium">{w.name}</span>
                          <p className="text-xs text-gray-500 mt-0.5 truncate">{w.seed_prompt.substring(0, 60)}</p>
                        </button>
                      ))}
                    </div>

                    {selectedWorld && (
                      <div className="space-y-2 pt-2 border-t border-gray-700">
                        <p className="text-xs text-gray-400">Starting location preference (optional)</p>
                        <div className="flex gap-2">
                          <input
                            value={startPreference}
                            onChange={e => setStartPreference(e.target.value)}
                            placeholder="e.g., coastal trading city"
                            className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                          />
                          <button
                            onClick={handlePickStart}
                            disabled={!startPreference.trim() || pickingStart}
                            className="px-3 py-1.5 rounded-lg bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-xs font-medium transition-colors"
                          >
                            {pickingStart ? 'Picking...' : 'Pick for me'}
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
                )}

                {characters.length > 0 && (
                  <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30">
                    <h4 className="text-sm font-medium text-gray-300 mb-2">Select a Character (optional)</h4>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                      <button
                        onClick={() => setSelectedCharacter(null)}
                        className={`p-3 rounded-lg border text-sm text-left transition-colors ${
                          !selectedCharacter
                            ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                            : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                        }`}
                      >
                        <span className="font-medium">Default Adventurer</span>
                        <p className="text-xs text-gray-500 mt-0.5">Start with basic stats</p>
                      </button>
                      {characters.map(c => (
                        <button
                          key={c.id}
                          onClick={() => setSelectedCharacter(c)}
                          className={`p-3 rounded-lg border text-sm text-left transition-colors ${
                            selectedCharacter?.id === c.id
                              ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                              : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                          }`}
                        >
                          <span className="font-medium">{c.name}</span>
                          <p className="text-xs text-gray-500 mt-0.5">{c.has_world ? 'World-themed' : 'Generic'} character</p>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
