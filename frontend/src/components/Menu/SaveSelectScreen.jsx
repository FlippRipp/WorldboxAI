import { useState, useEffect, useRef } from 'react';
import { api } from '../../lib/api';
import ModuleTogglePanel from '../shared/ModuleTogglePanel';
import ModuleInline from '../shared/ModuleInline';
import ModuleInstructionsEditor from '../shared/ModuleInstructionsEditor';
import BranchNameDialog from '../shared/BranchNameDialog';

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
  // The module-contributed story source (e.g. wb_worldgen world):
  //   null | { type:'world', id, startPreference, startLocation }
  // Modules report themselves via onSelect.
  const [storySource, setStorySource] = useState(null);
  // Scenario selection is independent of the story source: it can be used
  // alone or combined with a world, in which case the world supplies the
  // setting and the scenario supplies the opening message.
  //   null | { id, modificationRequest }
  const [selectedScenario, setSelectedScenario] = useState(null);
  // Per-module instruction overrides for the new story ({mod_id: {slot_id:
  // text}}). Pre-filled from the selected scenario and editable before
  // creation; the scenario's own values are kept for per-field reset.
  const [moduleInstructions, setModuleInstructions] = useState({});
  const [scenarioInstructionDefaults, setScenarioInstructionDefaults] = useState(null);
  // Optional Plot Director preferences (comma-separated), seeded into the
  // story's plot profile as player-set entries. Only shown/sent when the
  // plot module is active for the new story.
  const [plotLikes, setPlotLikes] = useState('');
  const [plotDislikes, setPlotDislikes] = useState('');
  const [creating, setCreating] = useState(false);
  // Ref mirror of `creating`: state hasn't flushed yet when a second Enter
  // keydown lands in the same tick, so the guard needs a synchronous check.
  const creatingRef = useRef(false);
  const [loadingSave, setLoadingSave] = useState(null);
  // 'list' shows the saves + "Create New Story"; 'create' shows the new-story form.
  const [view, setView] = useState('list');
  // Per-save settings editor (name, modules, export, branch).
  const [editingSave, setEditingSave] = useState(null);
  const [editModules, setEditModules] = useState(() => new Set());
  const [editName, setEditName] = useState('');
  // Story direction (themes/tags/pacing) injected into every turn's prompt.
  const [editStyle, setEditStyle] = useState({ themes: '', tags: '', pacing: '' });
  // Per-module instruction overrides of the save being edited, plus the
  // values frozen from its scenario at creation (the reset baseline).
  const [editInstructions, setEditInstructions] = useState({});
  const [editScenarioDefaults, setEditScenarioDefaults] = useState(null);
  const [savingSettings, setSavingSettings] = useState(false);
  const [branching, setBranching] = useState(false);
  // Branch naming dialog: { defaultName } | null. Branching always goes
  // through it; the input is prefilled with the auto-generated name.
  const [branchPrompt, setBranchPrompt] = useState(null);
  const [branchError, setBranchError] = useState(null);
  // Inline error messages, one per section, instead of blocking alert()s.
  const [loadError, setLoadError] = useState(false);
  const [listError, setListError] = useState(null);
  const [createError, setCreateError] = useState(null);
  const [settingsError, setSettingsError] = useState(null);

  const loadData = () => {
    setLoading(true);
    setLoadError(false);
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
      .catch(() => setLoadError(true))
      .finally(() => setLoading(false));
  };
  useEffect(loadData, []);

  // Escape closes the settings modal (unless a save is in flight).
  useEffect(() => {
    if (!editingSave) return;
    const onKey = (e) => {
      if (e.key === 'Escape' && !savingSettings) setEditingSave(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [editingSave, savingSettings]);

  const toggleModule = (modId, on) => {
    setEnabledModules((prev) => {
      const next = new Set(prev);
      if (on) next.add(modId); else next.delete(modId);
      return next;
    });
  };

  // Selecting a scenario pre-fills the module toggles and instruction fields
  // from it (still editable before creating); deselecting restores the
  // defaults. Re-clicking the already-selected scenario must not clobber the
  // user's edits.
  const selectScenario = async (id) => {
    if (id === null) {
      setSelectedScenario(null);
      setEnabledModules(new Set(modules.map((m) => m.id)));
      setModuleInstructions({});
      setScenarioInstructionDefaults(null);
      return;
    }
    if (selectedScenario?.id === id) return;
    setSelectedScenario({ id });
    try {
      const { scenario } = await api.loadScenario(id);
      setEnabledModules(new Set(
        Array.isArray(scenario.active_modules) ? scenario.active_modules : modules.map((m) => m.id)
      ));
      const instructions = scenario.module_instructions || {};
      setModuleInstructions(instructions);
      setScenarioInstructionDefaults(Object.keys(instructions).length ? instructions : null);
    } catch {
      // The scenario list may be stale; creation itself will surface the error.
    }
  };

  // Choosing a world that was created with a linked scenario pre-selects that
  // scenario (still deselectable). Only fires when the picked world actually
  // changes, so tweaking the start preference never re-links a scenario the
  // user deselected.
  const selectStorySource = (src) => {
    const prevWorldId = storySource?.type === 'world' ? storySource.id : null;
    setStorySource(src);
    if (src?.type === 'world' && src.id !== prevWorldId && src.linkedScenarioId) {
      selectScenario(src.linkedScenarioId);
    }
  };

  const toggleEditModule = (modId, on) => {
    setEditModules((prev) => {
      const next = new Set(prev);
      if (on) next.add(modId); else next.delete(modId);
      return next;
    });
  };

  const saveLabel = (saveId) =>
    saves.find((s) => s.id === saveId)?.display_name || saveId;

  const handleLoad = async (saveId) => {
    setLoadingSave(saveId);
    setListError(null);
    try {
      // Pass the loaded engine state through so the game can paint the
      // transcript immediately instead of waiting on the intro round-trip.
      const data = await api.loadSave(saveId);
      onLoad(saveId, data?.state);
    } catch (e) {
      setListError(`Failed to load "${saveLabel(saveId)}": ${e.message}`);
    }
    setLoadingSave(null);
  };

  const handleDelete = async (saveId) => {
    if (!window.confirm(`Delete "${saveLabel(saveId)}"? This cannot be undone.`)) return;
    setListError(null);
    try {
      await api.deleteSave(saveId);
      setSaves(prev => prev.filter(s => s.id !== saveId));
    } catch (e) {
      setListError(`Failed to delete "${saveLabel(saveId)}": ${e.message}`);
    }
  };

  const openSettings = async (saveId) => {
    setEditingSave(saveId);
    setSavingSettings(false);
    setBranching(false);
    setBranchPrompt(null);
    setBranchError(null);
    setSettingsError(null);
    const save = saves.find((s) => s.id === saveId);
    setEditName(save?.display_name || saveId);
    // Default to "all active" while we fetch, then apply the saved set. A null
    // result (legacy save) means every module is active.
    setEditModules(new Set(modules.map((m) => m.id)));
    setEditStyle({ themes: '', tags: '', pacing: '' });
    setEditInstructions({});
    setEditScenarioDefaults(null);
    try {
      const data = await api.getSaveActiveModules(saveId);
      const active = data.active_modules;
      if (Array.isArray(active)) setEditModules(new Set(active));
      const styleData = await api.getStoryStyle(saveId);
      const style = styleData.story_style || {};
      setEditStyle({ themes: style.themes || '', tags: style.tags || '', pacing: style.pacing || '' });
      const instrData = await api.getSaveModuleInstructions(saveId);
      setEditInstructions(instrData.module_instructions || {});
      const scenarioDefaults = instrData.scenario_module_instructions || {};
      setEditScenarioDefaults(Object.keys(scenarioDefaults).length ? scenarioDefaults : null);
    } catch (e) {
      setSettingsError(`Failed to load story settings: ${e.message}`);
    }
  };

  const handleSaveSettings = async () => {
    if (!editingSave) return;
    setSavingSettings(true);
    setSettingsError(null);
    try {
      await api.setSaveActiveModules(
        editingSave,
        modules.map((m) => m.id).filter((id) => editModules.has(id)),
      );
      await api.setStoryStyle(editingSave, editStyle);
      await api.setSaveModuleInstructions(editingSave, editInstructions);
      const save = saves.find((s) => s.id === editingSave);
      const name = editName.trim();
      if (name && name !== (save?.display_name || editingSave)) {
        await api.renameSave(editingSave, name);
        setSaves((prev) => prev.map((s) => (s.id === editingSave ? { ...s, display_name: name } : s)));
      }
      setEditingSave(null);
    } catch (e) {
      setSettingsError(`Failed to save settings: ${e.message}`);
    }
    setSavingSettings(false);
  };

  const handleBranch = () => {
    if (!editingSave) return;
    const save = saves.find((s) => s.id === editingSave);
    const sourceName = save?.display_name || editingSave;
    setBranchError(null);
    setBranchPrompt({ defaultName: `${sourceName} (branch @ turn ${save?.turn ?? 0})` });
  };

  const handleConfirmBranch = async (name) => {
    if (!editingSave) return;
    setBranching(true);
    setBranchError(null);
    try {
      const r = await api.branchSave(editingSave, { displayName: name });
      if (Array.isArray(r.saves)) setSaves(r.saves);
      setBranchPrompt(null);
      setEditingSave(null);
    } catch (e) {
      // Keep the dialog open so the player can retry or cancel.
      setBranchError(`Failed to branch: ${e.message}`);
    }
    setBranching(false);
  };

  const handleCreate = async () => {
    const name = newName.trim();
    // The guard also covers Enter on the name input, which bypasses the
    // disabled Create button.
    if (!name || creatingRef.current) return;
    creatingRef.current = true;
    setCreating(true);
    setCreateError(null);
    try {
      const isWorld = storySource?.type === 'world';
      // A pre-picked start location is sent by node id; only an unpicked
      // preference is sent as text (the backend then picks via LLM). When a
      // scenario is combined with the world, the start-location UI is hidden
      // and the backend derives the start from the scenario's opening — any
      // lingering pick/preference from before the scenario was selected must
      // not be sent.
      const scenarioChosen = !!selectedScenario;
      const pickedNodeId = isWorld && !scenarioChosen
        ? (storySource.startLocation?.node_id || null)
        : null;
      const pref = isWorld && !scenarioChosen && !pickedNodeId
        ? (storySource.startPreference?.trim() || null)
        : null;
      await api.createSave(name, {
        worldId: isWorld ? storySource.id : null,
        scenarioId: selectedScenario?.id || null,
        startPreference: pref,
        startLocationNodeId: pickedNodeId,
        scenarioRequest: selectedScenario?.modificationRequest?.trim() || null,
        characterId: selectedCharacter ? selectedCharacter.id : null,
        activeModules: modules.map((m) => m.id).filter((id) => enabledModules.has(id)),
        moduleInstructions: Object.keys(moduleInstructions).length ? moduleInstructions : null,
        plotLikes: enabledModules.has('wb_plot_director') ? (plotLikes.trim() || null) : null,
        plotDislikes: enabledModules.has('wb_plot_director') ? (plotDislikes.trim() || null) : null,
      });
      // The backend turns spaces into underscores for the save id; mirror
      // that here so the id we hand over matches the created save.
      onLoad(name.replace(/\s+/g, '_'));
    } catch (e) {
      setCreateError(`Failed to create save: ${e.message}`);
      creatingRef.current = false;
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
            ) : loadError ? (
              <div className="flex items-center justify-between gap-3 p-4 rounded-lg border border-red-800/50 bg-red-900/20">
                <p className="text-sm text-red-400">Couldn't load your stories. Is the server running?</p>
                <button
                  onClick={loadData}
                  className="shrink-0 px-4 py-2 rounded-lg border border-gray-700 hover:bg-gray-800 text-sm text-gray-300 transition-colors"
                >
                  Retry
                </button>
              </div>
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

                {listError && (
                  <p className="text-sm text-red-400 mb-4">{listError}</p>
                )}

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
                            className="px-4 py-1.5 min-h-[44px] rounded-lg bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-sm transition-colors"
                          >
                            {loadingSave === save.id ? 'Loading...' : 'Load'}
                          </button>
                          <button
                            onClick={() => openSettings(save.id)}
                            disabled={loadingSave === save.id}
                            className="p-1.5 min-h-[44px] min-w-[44px] flex items-center justify-center rounded-lg bg-gray-700/60 hover:bg-gray-700 border border-gray-600/50 hover:border-gray-500 disabled:opacity-50 text-gray-300 transition-colors"
                            title="Story settings"
                            aria-label="Story settings"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                            </svg>
                          </button>
                          <button
                            onClick={() => handleDelete(save.id)}
                            disabled={loadingSave === save.id}
                            className="p-1.5 min-h-[44px] min-w-[44px] flex items-center justify-center rounded-lg bg-red-900/50 hover:bg-red-800 border border-red-800/50 hover:border-red-700 disabled:opacity-50 text-red-300 transition-colors"
                            title="Delete save"
                            aria-label="Delete save"
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
                  placeholder="My Story"
                  onKeyDown={e => { if (e.key === 'Enter') handleCreate(); }}
                  className="flex-1 min-h-[44px] bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                  aria-label="New save name"
                />
                <button
                  onClick={handleCreate}
                  disabled={!newName.trim() || creating}
                  aria-busy={creating}
                  className="px-6 py-2 min-h-[44px] rounded-lg bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-sm font-medium transition-colors"
                >
                  {creating ? 'Creating...' : 'Create'}
                </button>
              </div>
              {createError && (
                <p className="text-sm text-red-400">{createError}</p>
              )}

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
                    onSelect={selectStorySource}
                    scenarioSelected={!!selectedScenario}
                  />
                ))}

              {scenarios.length > 0 && (
                <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30">
                  <h4 className="text-sm font-medium text-gray-300 mb-2">Select a Scenario (optional)</h4>
                  <p className="text-xs text-gray-500 mb-2">
                    A simple starting scenario. Can be combined with a world: the world provides the setting, the scenario provides the opening.
                  </p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    <button
                      onClick={() => selectScenario(null)}
                      aria-pressed={!selectedScenario}
                      className={`p-3 rounded-lg border text-sm text-left transition-colors ${
                        !selectedScenario
                          ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                          : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                      }`}
                    >
                      <span className="font-medium">{!selectedScenario && '✓ '}No Scenario</span>
                      <p className="text-xs text-gray-500 mt-0.5">Blank canvas</p>
                    </button>
                    {scenarios.map(s => (
                      <button
                        key={s.id}
                        onClick={() => selectScenario(s.id)}
                        aria-pressed={selectedScenario?.id === s.id}
                        className={`p-3 rounded-lg border text-sm text-left transition-colors ${
                          selectedScenario?.id === s.id
                            ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                            : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                        }`}
                      >
                        <span className="font-medium">{selectedScenario?.id === s.id && '✓ '}{s.name}</span>
                        <p className="text-xs text-gray-500 mt-0.5">
                          {s.has_starting_prompt ? 'Has opening message' : 'AI-generated opening'}
                        </p>
                      </button>
                    ))}
                  </div>
                  {selectedScenario && storySource?.type === 'world' && (
                    <p className="text-xs text-purple-400/70 italic mt-2">
                      This scenario will open the story inside the selected world.
                    </p>
                  )}
                  {selectedScenario && (
                    <div className="space-y-2 pt-2 mt-3 border-t border-gray-700">
                      <p className="text-xs text-gray-400">Request changes to this scenario (optional)</p>
                      <input
                        value={selectedScenario.modificationRequest || ''}
                        onChange={(e) => setSelectedScenario({ id: selectedScenario.id, modificationRequest: e.target.value })}
                        placeholder="e.g., set it in winter, add a rival"
                        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                      />
                      {selectedScenario.modificationRequest?.trim() ? (
                        <p className="text-xs text-purple-400/70 italic">The AI will adapt the scenario and its opening message to your request.</p>
                      ) : (
                        <p className="text-xs text-gray-500 italic">Leave empty to start the scenario as written.</p>
                      )}
                    </div>
                  )}
                </div>
              )}

              {modules.some((m) => m.has_instruction_slots && enabledModules.has(m.id)) && (
                <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30">
                  <h4 className="text-sm font-medium text-gray-300 mb-2">Module Instructions (optional)</h4>
                  <p className="text-xs text-gray-500 mb-3">
                    Customize how modules generate for this story — e.g. what kinds of skills the RPG
                    module proposes, or how actions are judged for XP.
                    {selectedScenario ? ' Pre-filled from the selected scenario; edit freely.' : ''}
                  </p>
                  <ModuleInstructionsEditor
                    modules={modules}
                    enabledModules={enabledModules}
                    value={moduleInstructions}
                    onChange={setModuleInstructions}
                    scenarioDefaults={scenarioInstructionDefaults}
                  />
                </div>
              )}

              {enabledModules.has('wb_plot_director') && (
                <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30">
                  <h4 className="text-sm font-medium text-gray-300 mb-2">🎭 Plot Preferences (optional)</h4>
                  <p className="text-xs text-gray-500 mb-3">
                    Tell the Plot Director what you enjoy or want kept out. These count as
                    your own stated preferences: plot threads lean into likes, treat dislikes
                    as hard no-gos, and both survive plot resets. Separate entries with commas.
                  </p>
                  <div className="space-y-2">
                    <div>
                      <label className="text-xs text-gray-400 block mb-1" htmlFor="plot-likes-input">Likes</label>
                      <input
                        id="plot-likes-input"
                        value={plotLikes}
                        onChange={(e) => setPlotLikes(e.target.value)}
                        placeholder="e.g., political intrigue, found family, sea voyages"
                        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                      />
                    </div>
                    <div>
                      <label className="text-xs text-gray-400 block mb-1" htmlFor="plot-dislikes-input">Dislikes</label>
                      <input
                        id="plot-dislikes-input"
                        value={plotDislikes}
                        onChange={(e) => setPlotDislikes(e.target.value)}
                        placeholder="e.g., body horror, romance subplots"
                        className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                      />
                    </div>
                  </div>
                </div>
              )}

              {characters.length > 0 && (
                <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30">
                  <h4 className="text-sm font-medium text-gray-300 mb-2">Select a Character (optional)</h4>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    <button
                      onClick={() => setSelectedCharacter(null)}
                      aria-pressed={!selectedCharacter}
                      className={`p-3 rounded-lg border text-sm text-left transition-colors ${
                        !selectedCharacter
                          ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                          : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                      }`}
                    >
                      <span className="font-medium">{!selectedCharacter && '✓ '}Default Adventurer</span>
                      <p className="text-xs text-gray-500 mt-0.5">Start with basic stats</p>
                    </button>
                    {characters.map(c => (
                      <button
                        key={c.id}
                        onClick={() => setSelectedCharacter(c)}
                        aria-pressed={selectedCharacter?.id === c.id}
                        className={`p-3 rounded-lg border text-sm text-left transition-colors ${
                          selectedCharacter?.id === c.id
                            ? 'border-purple-500 bg-purple-900/30 text-purple-200'
                            : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
                        }`}
                      >
                        <span className="font-medium">{selectedCharacter?.id === c.id && '✓ '}{c.name}</span>
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
            role="dialog"
            aria-modal="true"
            aria-label="Story settings"
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

            <div className="p-4 border-b border-gray-700">
              <h4 className="text-sm font-medium text-gray-300 mb-1">Story Style</h4>
              <p className="text-xs text-gray-500 mb-3">
                Optional direction the storyteller follows every turn. Leave a field empty for no direction.
              </p>
              <div className="space-y-2.5">
                {[
                  { key: 'themes', label: 'Themes', placeholder: 'e.g. redemption, found family, the cost of power' },
                  { key: 'tags', label: 'Tags', placeholder: 'e.g. dark fantasy, mystery, slow burn' },
                  { key: 'pacing', label: 'Pacing', placeholder: 'e.g. slow and atmospheric, fast-paced action' },
                ].map(({ key, label, placeholder }) => (
                  <div key={key}>
                    <label className="block text-xs text-gray-400 mb-1">{label}</label>
                    <input
                      value={editStyle[key]}
                      onChange={(e) => setEditStyle({ ...editStyle, [key]: e.target.value })}
                      placeholder={placeholder}
                      className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-purple-500"
                      aria-label={`Story ${label.toLowerCase()}`}
                    />
                  </div>
                ))}
              </div>
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

            {modules.some((m) => m.has_instruction_slots && editModules.has(m.id)) && (
            <div className="p-4 border-b border-gray-700">
              <h4 className="text-sm font-medium text-gray-300 mb-1">Module Instructions</h4>
              <p className="text-xs text-gray-500 mb-3">
                Customize how modules generate for this story. Empty fields use the
                {editScenarioDefaults ? " scenario's" : ''} defaults.
              </p>
              <ModuleInstructionsEditor
                modules={modules}
                enabledModules={editModules}
                value={editInstructions}
                onChange={setEditInstructions}
                scenarioDefaults={editScenarioDefaults}
              />
            </div>
            )}

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

            {settingsError && (
              <p className="text-sm text-red-400 px-4 pt-3">{settingsError}</p>
            )}
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

      {branchPrompt && (
        <BranchNameDialog
          defaultName={branchPrompt.defaultName}
          busy={branching}
          error={branchError}
          onConfirm={handleConfirmBranch}
          onCancel={() => !branching && setBranchPrompt(null)}
        />
      )}
    </div>
  );
}
