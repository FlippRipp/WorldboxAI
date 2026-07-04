import { useState, useEffect } from 'react';
import { api } from '../../lib/api';
import ModuleTogglePanel from '../shared/ModuleTogglePanel';
import ModuleInline from '../shared/ModuleInline';

function formatLastPlayed(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d)) return null;
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export default function SaveSelectScreen({ onLoad, onCreate, onBack }) {
  const [saves, setSaves] = useState([]);
  const [scenarios, setScenarios] = useState([]);
  const [modules, setModules] = useState([]);
  const [enabledModules, setEnabledModules] = useState(() => new Set());
  const [loading, setLoading] = useState(true);
  const [characters, setCharacters] = useState([]);
  const [selectedCharacter, setSelectedCharacter] = useState(null);
  const [newName, setNewName] = useState('');
  // The chosen story source (single selection):
  //   null | { type:'world', id, startPreference, startLocation } | { type:'scenario', id, modificationRequest }
  // Module-contributed sources (e.g. world) report themselves via onSelect.
  const [storySource, setStorySource] = useState(null);
  const [creating, setCreating] = useState(false);
  const [loadingSave, setLoadingSave] = useState(null);
  // 'list' shows the saves + "Create New Story"; 'create' shows the new-story form.
  const [view, setView] = useState('list');
  // Per-save settings editor (name, modules, export, branch).
  const [editingSave, setEditingSave] = useState(null);
  const [editModules, setEditModules] = useState(() => new Set());
  const [editName, setEditName] = useState('');
  const [savingSettings, setSavingSettings] = useState(false);
  const [branching, setBranching] = useState(false);

  useEffect(() => {
    Promise.all([
      api.getSaves(),
      api.listCharacters().catch(() => ({ characters: [] })),
      api.listScenarios().catch(() => ({ scenarios: [] })),
      api.getModules().catch(() => ({ modules: [] })),
    ])
      .then(([savesData, charsData, scenariosData, modulesData]) => {
        setSaves(savesData.saves || []);
        setCharacters(charsData.characters || []);
        setScenarios(scenariosData.scenarios || []);
        const mods = modulesData.modules || [];
        setModules(mods);
        setEnabledModules(new Set(mods.map((m) => m.id))); // all active by default
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const toggleModule = (modId, on) => {
    setEnabledModules((prev) => {
      const next = new Set(prev);
      if (on) next.add(modId); else next.delete(modId);
      return next;
    });
  };

  const toggleEditModule = (modId, on) => {
    setEditModules((prev) => {
      const next = new Set(prev);
      if (on) next.add(modId); else next.delete(modId);
      return next;
    });
  };

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

  const handleDelete = async (saveId) => {
    if (!window.confirm(`Delete "${saveId}"? This cannot be undone.`)) return;
    try {
      await api.deleteSave(saveId);
      setSaves(prev => prev.filter(s => s.id !== saveId));
    } catch (e) {
      alert(`Failed to delete: ${e.message}`);
    }
  };

  const openSettings = async (saveId) => {
    setEditingSave(saveId);
    setSavingSettings(false);
    setBranching(false);
    const save = saves.find((s) => s.id === saveId);
    setEditName(save?.display_name || saveId);
    // Default to "all active" while we fetch, then apply the saved set. A null
    // result (legacy save) means every module is active.
    setEditModules(new Set(modules.map((m) => m.id)));
    try {
      const data = await api.getSaveActiveModules(saveId);
      const active = data.active_modules;
      if (Array.isArray(active)) setEditModules(new Set(active));
    } catch (e) {
      alert(`Failed to load story settings: ${e.message}`);
    }
  };

  const handleSaveSettings = async () => {
    if (!editingSave) return;
    setSavingSettings(true);
    try {
      await api.setSaveActiveModules(
        editingSave,
        modules.map((m) => m.id).filter((id) => editModules.has(id)),
      );
      const save = saves.find((s) => s.id === editingSave);
      const name = editName.trim();
      if (name && name !== (save?.display_name || editingSave)) {
        await api.renameSave(editingSave, name);
        setSaves((prev) => prev.map((s) => (s.id === editingSave ? { ...s, display_name: name } : s)));
      }
      setEditingSave(null);
    } catch (e) {
      alert(`Failed to save settings: ${e.message}`);
    }
    setSavingSettings(false);
  };

  const handleBranch = async () => {
    if (!editingSave) return;
    setBranching(true);
    try {
      const r = await api.branchSave(editingSave);
      if (Array.isArray(r.saves)) setSaves(r.saves);
      setEditingSave(null);
    } catch (e) {
      alert(`Failed to branch: ${e.message}`);
    }
    setBranching(false);
  };

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) return;
    setCreating(true);
    try {
      const isWorld = storySource?.type === 'world';
      const isScenario = storySource?.type === 'scenario';
      // A pre-picked start location means we don't re-send a preference.
      const pref = isWorld
        ? (storySource.startLocation ? null : (storySource.startPreference?.trim() || null))
        : null;
      await api.createSave(name, {
        worldId: isWorld ? storySource.id : null,
        scenarioId: isScenario ? storySource.id : null,
        startPreference: pref,
        scenarioRequest: isScenario ? (storySource.modificationRequest?.trim() || null) : null,
        characterId: selectedCharacter ? selectedCharacter.id : null,
        activeModules: modules.map((m) => m.id).filter((id) => enabledModules.has(id)),
      });
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
          onClick={view === 'create' ? () => setView('list') : onBack}
          className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-8"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          {view === 'create' ? 'Back to Stories' : 'Back to Menu'}
        </button>

        {view === 'list' ? (
          <>
            <h2 className="text-3xl font-bold text-gray-100 mb-2">Storyteller</h2>
            <p className="text-gray-500 text-sm mb-8">Pick a story to continue, or start a new one.</p>

            {loading ? (
              <div className="text-gray-500 text-center py-12">Loading stories...</div>
            ) : (
              <>
                <button
                  onClick={() => setView('create')}
                  className="w-full flex items-center justify-center gap-2 mb-6 px-4 py-3 rounded-lg bg-purple-700 hover:bg-purple-600 font-medium transition-colors"
                >
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                  </svg>
                  Create New Story
                </button>

                {saves.length > 0 ? (
                  <div className="space-y-2">
                    {saves.map(save => (
                      <div
                        key={save.id}
                        className="flex items-center justify-between p-4 rounded-lg border border-gray-700 bg-gray-800/50 hover:bg-gray-800 transition-colors"
                      >
                        <div className="flex items-center gap-3 min-w-0">
                          <span className="text-xl">📁</span>
                          <div className="min-w-0">
                            <h4 className="font-medium text-gray-200 truncate">{save.display_name || save.id}</h4>
                            <p className="text-xs text-gray-500 truncate">
                              {[
                                save.active ? 'Active session' : null,
                                `Turn ${save.turn ?? 0}`,
                                formatLastPlayed(save.last_played),
                              ].filter(Boolean).join(' · ')}
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
                            onClick={() => openSettings(save.id)}
                            disabled={loadingSave === save.id}
                            className="p-1.5 rounded-lg bg-gray-700/60 hover:bg-gray-700 border border-gray-600/50 hover:border-gray-500 disabled:opacity-50 text-gray-300 transition-colors"
                            title="Story settings"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                            </svg>
                          </button>
                          <button
                            onClick={() => handleDelete(save.id)}
                            disabled={loadingSave === save.id}
                            className="p-1.5 rounded-lg bg-red-900/50 hover:bg-red-800 border border-red-800/50 hover:border-red-700 disabled:opacity-50 text-red-300 transition-colors"
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
                ) : (
                  <p className="text-gray-500 text-center py-8 border border-dashed border-gray-700 rounded-lg">
                    No stories yet. Create one above.
                  </p>
                )}
              </>
            )}
          </>
        ) : (
          <>
            <h2 className="text-3xl font-bold text-gray-100 mb-2">Create New Story</h2>
            <p className="text-gray-500 text-sm mb-8">Name your story and choose how it begins.</p>

            <div className="flex items-center justify-between mb-3">
              <h3 className="text-lg font-semibold text-gray-200">Story Setup</h3>
              <ModuleTogglePanel modules={modules} enabled={enabledModules} onToggle={toggleModule} label="Active Modules" />
            </div>
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

              {/* Module-contributed story sources (e.g. wb_worldgen world
                  select). Only shown for enabled modules, so toggling a module
                  off removes its story-source UI. Each is a controlled picker:
                  reporting a source replaces any previous selection. */}
              {modules
                .filter((m) => m.storyteller_start && enabledModules.has(m.id))
                .map((m) => (
                  <ModuleInline
                    key={m.id}
                    modId={m.id}
                    file={m.storyteller_start.screen}
                    selected={storySource}
                    onSelect={setStorySource}
                  />
                ))}

              {scenarios.length > 0 && (
                <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30">
                  <h4 className="text-sm font-medium text-gray-300 mb-2">Select a Scenario (optional)</h4>
                  <p className="text-xs text-gray-500 mb-2">A simple starting scenario. Selecting one clears any other story source.</p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    <button
                      onClick={() => { if (storySource?.type === 'scenario') setStorySource(null); }}
                      className={`p-3 rounded-lg border text-sm text-left transition-colors ${
                        storySource?.type !== 'scenario'
                          ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                          : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                      }`}
                    >
                      <span className="font-medium">No Scenario</span>
                      <p className="text-xs text-gray-500 mt-0.5">Blank canvas</p>
                    </button>
                    {scenarios.map(s => (
                      <button
                        key={s.id}
                        onClick={() => setStorySource({ type: 'scenario', id: s.id })}
                        className={`p-3 rounded-lg border text-sm text-left transition-colors ${
                          storySource?.type === 'scenario' && storySource.id === s.id
                            ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                            : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                        }`}
                      >
                        <span className="font-medium">{s.name}</span>
                        <p className="text-xs text-gray-500 mt-0.5">
                          {s.has_starting_prompt ? 'Has opening message' : 'AI-generated opening'}
                        </p>
                      </button>
                    ))}
                  </div>
                  {storySource?.type === 'scenario' && (
                    <div className="space-y-2 pt-2 mt-3 border-t border-gray-700">
                      <p className="text-xs text-gray-400">Request changes to this scenario (optional)</p>
                      <input
                        value={storySource.modificationRequest || ''}
                        onChange={(e) => setStorySource({ type: 'scenario', id: storySource.id, modificationRequest: e.target.value })}
                        placeholder="e.g., set it in winter, add a rival"
                        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                      />
                      {storySource.modificationRequest?.trim() ? (
                        <p className="text-xs text-purple-400/70 italic">The AI will adapt the scenario and its opening message to your request.</p>
                      ) : (
                        <p className="text-xs text-gray-500 italic">Leave empty to start the scenario as written.</p>
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
                        <p className="text-xs text-gray-500 mt-0.5">{c.has_context ? 'Themed' : 'Generic'} character</p>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {editingSave && (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4"
          onClick={() => !savingSettings && setEditingSave(null)}
        >
          <div
            className="w-full max-w-md rounded-xl border border-gray-700 bg-gray-900 shadow-2xl max-h-[85vh] overflow-y-auto book-scroll"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between p-4 border-b border-gray-700">
              <div>
                <h3 className="text-lg font-semibold text-gray-100">Story Settings</h3>
                <p className="text-xs text-gray-500 mt-0.5 truncate">{editingSave}</p>
              </div>
              <button
                onClick={() => !savingSettings && setEditingSave(null)}
                className="text-gray-500 hover:text-gray-300 transition-colors"
                aria-label="Close"
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="p-4 border-b border-gray-700">
              <h4 className="text-sm font-medium text-gray-300 mb-1">Story Name</h4>
              <p className="text-xs text-gray-500 mb-2">Display name only — files keep the original id.</p>
              <input
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                maxLength={120}
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-purple-500"
                aria-label="Story display name"
              />
            </div>

            <div className="p-4 border-b border-gray-700 flex items-start justify-between gap-4">
              <div>
                <h4 className="text-sm font-medium text-gray-300 mb-1">Export</h4>
                <p className="text-xs text-gray-500">Download the transcript.</p>
              </div>
              <div className="flex gap-2 shrink-0">
                {['md', 'txt', 'jsonl'].map((fmt) => (
                  <a
                    key={fmt}
                    href={api.exportSaveUrl(editingSave, fmt)}
                    download
                    className="px-3 py-1.5 rounded-lg border border-gray-700 hover:bg-gray-800 text-xs text-gray-300 uppercase transition-colors"
                  >
                    {fmt}
                  </a>
                ))}
              </div>
            </div>

            <div className="p-4 border-b border-gray-700 flex items-start justify-between gap-4">
              <div>
                <h4 className="text-sm font-medium text-gray-300 mb-1">Branch</h4>
                <p className="text-xs text-gray-500">Fork this story into a new save at its current turn.</p>
              </div>
              <button
                onClick={handleBranch}
                disabled={branching || savingSettings}
                className="shrink-0 px-4 py-1.5 rounded-lg border border-gray-700 hover:bg-gray-800 disabled:opacity-50 text-xs text-gray-300 transition-colors"
              >
                {branching ? 'Branching…' : 'Create Branch'}
              </button>
            </div>

            <div className="p-4">
              <h4 className="text-sm font-medium text-gray-300 mb-1">Active Modules</h4>
              <p className="text-xs text-gray-500 mb-3">
                Choose which modules this story uses. Disabled modules contribute no UI or behavior.
              </p>
              {modules.length === 0 ? (
                <p className="text-xs text-gray-500">No modules loaded.</p>
              ) : (
                <div className="space-y-1 max-h-72 overflow-y-auto">
                  {modules.map((m) => {
                    const on = editModules.has(m.id);
                    return (
                      <button
                        key={m.id}
                        onClick={() => toggleEditModule(m.id, !on)}
                        className="w-full flex items-center justify-between px-3 py-2 rounded-lg border border-gray-700 bg-gray-800/50 hover:bg-gray-800 transition-colors text-left"
                      >
                        <span className="text-sm text-gray-200 truncate">{m.name || m.id}</span>
                        <span className={`shrink-0 w-9 h-5 rounded-full relative transition-colors ${on ? 'bg-purple-600' : 'bg-gray-600'}`}>
                          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all ${on ? 'left-[1.125rem]' : 'left-0.5'}`} />
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            <div className="flex items-center justify-end gap-2 p-4 border-t border-gray-700">
              <button
                onClick={() => setEditingSave(null)}
                disabled={savingSettings}
                className="px-4 py-2 rounded-lg border border-gray-700 hover:bg-gray-800 disabled:opacity-50 text-sm text-gray-300 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleSaveSettings}
                disabled={savingSettings}
                className="px-5 py-2 rounded-lg bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-sm font-medium transition-colors"
              >
                {savingSettings ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
