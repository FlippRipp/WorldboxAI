import { useState, useEffect } from 'react';
import { api } from '../../lib/api';

// Simple manager for "basic scenarios" — the default story source. A scenario
// is just a starting prompt (the literal first AI message) plus a scenario
// description (the system prompt that frames the story for the AI). This is the
// lightweight counterpart to the full world-generation wizard.
export default function ScenarioManager({ onBack }) {
  const [scenarios, setScenarios] = useState([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(null); // {id?, name, scenario_description, starting_prompt}
  const [saving, setSaving] = useState(false);
  const [allLorebooks, setAllLorebooks] = useState([]);
  const [linkedLorebooks, setLinkedLorebooks] = useState([]);

  const refresh = () => {
    setLoading(true);
    api.listScenarios()
      .then((d) => setScenarios(d.scenarios || []))
      .catch(() => {})
      .finally(() => setLoading(false));
    api.listLorebooks()
      .then((d) => setAllLorebooks(d.lorebooks || []))
      .catch(() => {});
  };

  useEffect(refresh, []);

  const startNew = () => {
    setLinkedLorebooks([]);
    setEditing({ name: '', scenario_description: '', starting_prompt: '', themes: '', tags: '', pacing: '' });
  };

  const startEdit = async (id) => {
    try {
      const { scenario } = await api.loadScenario(id);
      const { lorebook_ids } = await api.getLorebookLinks('scenario', id).catch(() => ({ lorebook_ids: [] }));
      setLinkedLorebooks(lorebook_ids || []);
      // Older scenarios predate themes/tags/pacing; backfill so the inputs stay controlled.
      setEditing({ themes: '', tags: '', pacing: '', ...scenario });
    } catch (e) {
      alert(`Failed to load scenario: ${e.message}`);
    }
  };

  const handleSave = async () => {
    if (!editing?.name.trim()) return;
    setSaving(true);
    try {
      const { scenario } = await api.saveScenario(editing);
      // Links live outside the scenario record; a new scenario only gets its
      // id from the save call, so linking must follow it.
      await api.setLorebookLinks('scenario', scenario.id, linkedLorebooks);
      setEditing(null);
      refresh();
    } catch (e) {
      alert(`Failed to save scenario: ${e.message}`);
    }
    setSaving(false);
  };

  const handleDelete = async (id) => {
    if (!window.confirm('Delete this scenario? This cannot be undone.')) return;
    try {
      await api.deleteScenario(id);
      refresh();
    } catch (e) {
      alert(`Failed to delete: ${e.message}`);
    }
  };

  const field = (label, hint) => (
    <div className="flex items-baseline justify-between">
      <label className="text-sm font-medium text-gray-300">{label}</label>
      <span className="text-xs text-gray-500">{hint}</span>
    </div>
  );

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center p-6">
      <div className="w-full max-w-2xl">
        <button
          onClick={editing ? () => setEditing(null) : onBack}
          className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-8"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          {editing ? 'Back to Scenarios' : 'Back to Menu'}
        </button>

        {!editing ? (
          <>
            <div className="flex items-center justify-between mb-6">
              <div>
                <h2 className="text-3xl font-bold text-gray-100 mb-2">Scenarios</h2>
                <p className="text-gray-500 text-sm">A starting prompt and a scenario description — the simplest way to begin a story.</p>
              </div>
              <button
                onClick={startNew}
                className="px-4 py-2 rounded-lg bg-purple-700 hover:bg-purple-600 text-sm font-medium transition-colors"
              >
                + New Scenario
              </button>
            </div>

            {loading ? (
              <div className="text-gray-500 text-center py-12">Loading...</div>
            ) : scenarios.length === 0 ? (
              <p className="text-gray-500 text-center py-12 border border-dashed border-gray-700 rounded-lg">
                No scenarios yet. Create one to start a story without a generated world.
              </p>
            ) : (
              <div className="space-y-2">
                {scenarios.map((s) => (
                  <div key={s.id} className="flex items-center justify-between p-4 rounded-lg border border-gray-700 bg-gray-800/50">
                    <div className="flex items-center gap-3">
                      <span className="text-xl">🎬</span>
                      <div>
                        <h4 className="font-medium text-gray-200">{s.name}</h4>
                        <p className="text-xs text-gray-500">
                          {s.has_starting_prompt ? 'Has opening message' : 'Opening generated by AI'}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <button onClick={() => startEdit(s.id)} className="px-4 py-1.5 rounded-lg bg-gray-700 hover:bg-gray-600 text-sm transition-colors">Edit</button>
                      <button onClick={() => handleDelete(s.id)} className="px-3 py-1.5 rounded-lg bg-red-900/50 hover:bg-red-800 border border-red-800/50 text-sm text-red-300 transition-colors" title="Delete">
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        ) : (
          <>
            <h2 className="text-3xl font-bold text-gray-100 mb-6">{editing.id ? 'Edit Scenario' : 'New Scenario'}</h2>
            <div className="space-y-5">
              <div className="space-y-1.5">
                {field('Name', 'shown in the story picker')}
                <input
                  value={editing.name}
                  onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                  placeholder="The Lonely Tavern"
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                />
              </div>

              <div className="space-y-1.5">
                {field('Scenario description', 'system prompt — describes the setting to the AI')}
                <textarea
                  value={editing.scenario_description}
                  onChange={(e) => setEditing({ ...editing, scenario_description: e.target.value })}
                  rows={6}
                  placeholder="A rain-soaked frontier town where strangers gather at the only inn for miles..."
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500 resize-y"
                />
              </div>

              <div className="space-y-1.5">
                {field('Starting prompt', 'optional — the literal first AI message; leave blank to have the AI write the opening')}
                <textarea
                  value={editing.starting_prompt}
                  onChange={(e) => setEditing({ ...editing, starting_prompt: e.target.value })}
                  rows={5}
                  placeholder="The tavern door groans shut behind you, cutting off the storm..."
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500 resize-y"
                />
              </div>

              <div className="space-y-3 p-4 rounded-lg border border-gray-700 bg-gray-800/30">
                <div>
                  <h4 className="text-sm font-medium text-gray-300">Story Style</h4>
                  <p className="text-xs text-gray-500">
                    Optional direction injected into every turn to guide the story's themes, style, and pace. Editable per story after creation.
                  </p>
                </div>
                <div className="space-y-1.5">
                  {field('Themes', 'e.g. redemption, found family, the cost of power')}
                  <input
                    value={editing.themes}
                    onChange={(e) => setEditing({ ...editing, themes: e.target.value })}
                    placeholder="Empty — no theme direction"
                    className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                  />
                </div>
                <div className="space-y-1.5">
                  {field('Tags', 'e.g. dark fantasy, mystery, slow burn, political intrigue')}
                  <input
                    value={editing.tags}
                    onChange={(e) => setEditing({ ...editing, tags: e.target.value })}
                    placeholder="Empty — no tags"
                    className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                  />
                </div>
                <div className="space-y-1.5">
                  {field('Pacing', 'e.g. slow and atmospheric, fast-paced with frequent action')}
                  <input
                    value={editing.pacing}
                    onChange={(e) => setEditing({ ...editing, pacing: e.target.value })}
                    placeholder="Empty — default pacing"
                    className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500"
                  />
                </div>
              </div>

              {allLorebooks.length > 0 && (
                <div className="space-y-1.5">
                  {field('Lorebooks', 'stories created from this scenario include the checked lorebooks')}
                  <div className="space-y-1 p-3 rounded-lg border border-gray-700 bg-gray-800/50">
                    {allLorebooks.map((b) => (
                      <label key={b.id} className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={linkedLorebooks.includes(b.id)}
                          onChange={(e) => setLinkedLorebooks(
                            e.target.checked
                              ? [...linkedLorebooks, b.id]
                              : linkedLorebooks.filter((id) => id !== b.id)
                          )}
                          className="accent-purple-600"
                        />
                        📚 {b.name}
                        <span className="text-xs text-gray-500">({b.enabled_count}/{b.entry_count} entries)</span>
                      </label>
                    ))}
                  </div>
                </div>
              )}

              <div className="flex gap-2">
                <button
                  onClick={handleSave}
                  disabled={!editing.name.trim() || saving}
                  className="px-6 py-2 rounded-lg bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-sm font-medium transition-colors"
                >
                  {saving ? 'Saving...' : 'Save Scenario'}
                </button>
                <button onClick={() => setEditing(null)} className="px-6 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-sm transition-colors">
                  Cancel
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
