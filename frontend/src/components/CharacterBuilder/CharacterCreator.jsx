import { useState, useEffect, useRef, useCallback } from 'react';
import { api } from '../../lib/api';
import CharacterModuleForm from './CharacterModuleForm';

function AutoTextarea({ value, onChange, disabled, minRows = 3, placeholder }) {
  const ref = useRef(null);
  const adjustHeight = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.max(el.scrollHeight, minRows * 24) + 'px';
  }, [minRows]);
  useEffect(() => { adjustHeight(); }, [value, adjustHeight]);
  return (
    <textarea
      ref={ref}
      value={value}
      onChange={onChange}
      onInput={adjustHeight}
      disabled={disabled}
      placeholder={placeholder}
      rows={minRows}
      className="w-full bg-gray-900 border border-gray-700 rounded-lg px-4 py-3 text-gray-200 focus:border-purple-500 focus:outline-none resize-none overflow-hidden whitespace-pre-wrap break-words"
    />
  );
}

export default function CharacterCreator({ onBack, onSaved, editCharacterId, initialData }) {
  const [worlds, setWorlds] = useState([]);
  const [modules, setModules] = useState([]);
  const [moduleDefaults, setModuleDefaults] = useState({});
  const [loading, setLoading] = useState(false);
  const [generatingName, setGeneratingName] = useState(false);
  const [generatingRace, setGeneratingRace] = useState(false);
  const [generatingAppearance, setGeneratingAppearance] = useState(false);
  const [generatingStats, setGeneratingStats] = useState(false);

  const [worldId, setWorldId] = useState('');
  const [gender, setGender] = useState('');
  const [race, setRace] = useState('');
  const [name, setName] = useState('');
  const [shortAppearance, setShortAppearance] = useState('');
  const [fullAppearance, setFullAppearance] = useState('');
  const [concept, setConcept] = useState('');
  const [backstory, setBackstory] = useState('');
  const [moduleData, setModuleData] = useState({});
  const [saveId, setSaveId] = useState('');

  useEffect(() => {
    api.getModules()
      .then(data => setModules(data.modules || []))
      .catch(() => {});

    api.listWorlds()
      .then(data => setWorlds(data.worlds || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (initialData && editCharacterId) {
      setWorldId(initialData.world_id || '');
      setGender(initialData.gender || '');
      setRace(initialData.race || '');
      setName(initialData.name || '');
      setShortAppearance(initialData.short_appearance || '');
      setFullAppearance(initialData.full_appearance || '');
      setModuleData(initialData.module_data || {});
      setBackstory(initialData.module_data?.wb_core_rpg?.backstory || '');
      setSaveId(initialData.id || '');
    }
  }, [initialData, editCharacterId]);

  useEffect(() => {
    api.getCharacterModuleDefaults(worldId || undefined)
      .then(data => {
        setModuleDefaults(data.module_defaults || {});
        if (!editCharacterId) {
          const combined = {};
          for (const modId of Object.keys(data.module_defaults || {})) {
            combined[modId] = data.module_defaults[modId];
          }
          if (Object.keys(combined).length > 0) {
            setModuleData(prev => Object.keys(prev).length === 0 ? combined : prev);
          }
        }
      })
      .catch(() => {});
  }, [worldId, editCharacterId]);

  const handleWorldChange = (e) => {
    setWorldId(e.target.value);
  };

  const handleGenerateName = async () => {
    setGeneratingName(true);
    try {
      const result = await api.generateCharacterName({
        world_id: worldId || null,
        gender,
        race,
      });
      setName(result.name || '');
    } catch (e) {
      alert('Failed to generate name: ' + e.message);
    } finally {
      setGeneratingName(false);
    }
  };

  const handleGenerateRace = async () => {
    setGeneratingRace(true);
    try {
      const result = await api.generateCharacterRace({
        world_id: worldId || null,
        gender,
      });
      setRace(result.race || '');
    } catch (e) {
      alert('Failed to generate race: ' + e.message);
    } finally {
      setGeneratingRace(false);
    }
  };

  const handleGenerateAppearance = async () => {
    if (!shortAppearance.trim()) return;
    setGeneratingAppearance(true);
    try {
      const result = await api.generateCharacterAppearance({
        short_description: shortAppearance,
        world_id: worldId || null,
        gender,
        race,
      });
      setFullAppearance(result.full_appearance || '');
    } catch (e) {
      alert('Failed to generate appearance: ' + e.message);
    } finally {
      setGeneratingAppearance(false);
    }
  };

  const handleGenerateStats = async () => {
    if (!concept.trim()) return;
    setGeneratingStats(true);
    try {
      const result = await api.generateCharacterStats({
        concept: concept.trim(),
        world_id: worldId || null,
        gender,
        race,
      });
      const vit = result.stats?.vitality ?? 10;
      const maxHp = vit * 7 + 2;
      setModuleData(prev => ({
        ...prev,
        wb_core_rpg: {
          stats: result.stats || {},
          skills: result.skills || {},
          backstory: result.backstory || '',
          level: 1,
          xp: 0,
          hp: maxHp,
          max_hp: maxHp,
        },
      }));
      if (result.backstory) {
        setBackstory(result.backstory);
      }
    } catch (e) {
      alert('Failed to generate stats: ' + e.message);
    } finally {
      setGeneratingStats(false);
    }
  };

  const handleSave = async () => {
    if (!name.trim()) {
      alert('Please enter a name for your character.');
      return;
    }
    setLoading(true);
    try {
      const characterId = saveId || name.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '');
      const result = await api.saveCharacter({
        id: characterId,
        name: name.trim(),
        gender: gender.trim(),
        race: race.trim(),
        short_appearance: shortAppearance.trim(),
        full_appearance: fullAppearance.trim(),
        world_id: worldId || null,
        module_data: moduleData,
      });
      if (result.saved) {
        onSaved?.();
      }
    } catch (e) {
      alert('Failed to save character: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleModuleChange = (modId, data) => {
    setModuleData(prev => ({ ...prev, [modId]: { ...prev[modId], ...data } }));
  };

  const characterModules = modules.filter(m => m.has_character_creation);

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col p-6">
      <div className="w-full max-w-2xl mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <button
            onClick={onBack}
            className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            Back
          </button>
          <h2 className="text-xl font-bold text-gray-100">
            {editCharacterId ? 'Edit Character' : 'New Character'}
          </h2>
        </div>

        <div className="bg-gray-800/60 border border-gray-700 rounded-xl p-6 space-y-4">
          <div>
            <h3 className="text-lg font-semibold text-gray-200 mb-1">World Context <span className="text-gray-500 text-sm font-normal">(optional)</span></h3>
            <p className="text-xs text-gray-500 mb-3">Select a world to help the AI generate theme-appropriate details.</p>
            <select
              value={worldId}
              onChange={handleWorldChange}
              className="w-full bg-gray-900 border border-gray-700 rounded-lg px-4 py-2.5 text-gray-200 focus:border-purple-500 focus:outline-none"
            >
              <option value="">None (generic character)</option>
              {worlds.map(w => (
                <option key={w.id} value={w.id}>{w.name}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="bg-gray-800/60 border border-gray-700 rounded-xl p-6 space-y-4">
          <h3 className="text-lg font-semibold text-gray-200">Basic Info</h3>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">
                Gender <span className="text-gray-600 text-xs">(optional)</span>
              </label>
              <input
                type="text"
                value={gender}
                onChange={(e) => setGender(e.target.value)}
                placeholder="e.g. female, male, non-binary..."
                className="w-full bg-gray-900 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 focus:border-purple-500 focus:outline-none"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">
                Race <span className="text-gray-600 text-xs">(optional)</span>
              </label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={race}
                  onChange={(e) => setRace(e.target.value)}
                  placeholder="e.g. elf, dwarf, human..."
                  className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 focus:border-purple-500 focus:outline-none"
                />
                <button
                  onClick={handleGenerateRace}
                  disabled={generatingRace}
                  className="px-4 py-2 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5 whitespace-nowrap"
                >
                  {generatingRace && (
                    <span className="inline-block w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  )}
                  {generatingRace ? '...' : 'Generate'}
                </button>
              </div>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">Name</label>
            <div className="flex gap-2">
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Enter a name or generate one..."
                className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-4 py-2 text-gray-200 focus:border-purple-500 focus:outline-none"
              />
              <button
                onClick={handleGenerateName}
                disabled={generatingName}
                className="px-4 py-2 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5 whitespace-nowrap"
              >
                {generatingName && (
                  <span className="inline-block w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                )}
                {generatingName ? '...' : 'Generate'}
              </button>
            </div>
          </div>
        </div>

        <div className="bg-gray-800/60 border border-gray-700 rounded-xl p-6 space-y-4">
          <h3 className="text-lg font-semibold text-gray-200">Appearance</h3>

          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">Short Description</label>
            <AutoTextarea
              value={shortAppearance}
              onChange={(e) => setShortAppearance(e.target.value)}
              placeholder="e.g. tall elf with silver hair and piercing green eyes, always wears a dark cloak..."
              disabled={generatingAppearance}
            />
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={handleGenerateAppearance}
              disabled={generatingAppearance || !shortAppearance.trim()}
              className="px-4 py-2 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5"
            >
              {generatingAppearance && (
                <span className="inline-block w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              )}
              Generate Full Appearance
            </button>
          </div>

          {fullAppearance && (
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Full Appearance</label>
              <AutoTextarea
                value={fullAppearance}
                onChange={(e) => setFullAppearance(e.target.value)}
                minRows={4}
                placeholder="Full appearance description will appear here..."
              />
            </div>
          )}
        </div>

        {characterModules.length > 0 && (
          <>
            <div className="bg-gray-800/60 border border-gray-700 rounded-xl p-6 space-y-4">
              <h3 className="text-lg font-semibold text-gray-200">Character Concept</h3>
              <p className="text-xs text-gray-500">Describe who your character is and what they are capable of. The AI will generate stats, skills, and a polished backstory.</p>

              <AutoTextarea
                value={concept}
                onChange={(e) => setConcept(e.target.value)}
                placeholder="e.g. A grizzled war veteran who relies on brute force and intimidation, but struggled with book learning. Years on the battlefield made them tough but wary of strangers..."
                disabled={generatingStats}
                minRows={3}
              />

              <button
                onClick={handleGenerateStats}
                disabled={generatingStats || !concept.trim()}
                className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5"
              >
                {generatingStats && (
                  <span className="inline-block w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                )}
                {generatingStats ? 'Generating...' : 'Generate Stats & Skills'}
              </button>

              {backstory && (
                <div>
                  <label className="block text-sm font-medium text-gray-400 mb-1">AI Backstory</label>
                  <AutoTextarea
                    value={backstory}
                    onChange={(e) => {
                      setBackstory(e.target.value);
                      setModuleData(prev => ({
                        ...prev,
                        wb_core_rpg: { ...prev.wb_core_rpg, backstory: e.target.value },
                      }));
                    }}
                    minRows={3}
                    placeholder="Backstory will appear here..."
                  />
                </div>
              )}
            </div>

            <div className="bg-gray-800/60 border border-gray-700 rounded-xl p-6 space-y-4">
              <h3 className="text-lg font-semibold text-gray-200">Modules</h3>
              {characterModules.map(mod => (
                <CharacterModuleForm
                  key={mod.id}
                  modId={mod.id}
                  value={moduleData[mod.id]}
                  onChange={(data) => handleModuleChange(mod.id, data)}
                  worldId={worldId}
                />
              ))}
            </div>
          </>
        )}

        {characterModules.length === 0 && (
          <div className="text-center text-gray-500 py-8">
            <p>No character modules loaded.</p>
          </div>
        )}

        <div className="flex justify-end">
          <button
            onClick={handleSave}
            disabled={loading || !name.trim()}
            className="px-6 py-3 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded-lg font-medium text-lg transition-colors flex items-center gap-2"
          >
            {loading && (
              <span className="inline-block w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
            )}
            {loading ? 'Saving...' : 'Save Character'}
          </button>
        </div>
      </div>
    </div>
  );
}
