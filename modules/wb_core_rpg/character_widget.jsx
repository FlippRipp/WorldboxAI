import React, { useState, useEffect } from 'react';

const STAT_NAMES = ['power', 'agility', 'vitality', 'intelligence', 'spirit', 'charm'];
const DEFAULT_STATS = { power: 10, agility: 10, vitality: 10, intelligence: 10, spirit: 10, charm: 10 };
const BASE_TOTAL = Object.values(DEFAULT_STATS).reduce((a, b) => a + b, 0);
const POINT_POOL = 12;

function StatAllocation({ stats, onChange }) {
  const totalSpent = STAT_NAMES.reduce((sum, s) => sum + (stats[s] || 10), 0);
  const remaining = POINT_POOL - (totalSpent - BASE_TOTAL);

  const handleStatChange = (stat, delta) => {
    const current = stats[stat] || 10;
    const next = current + delta;
    if (next < 1 || next > 30) return;
    const nextTotal = totalSpent + delta;
    if (nextTotal - BASE_TOTAL > POINT_POOL) return;
    onChange({ ...stats, [stat]: next });
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-medium text-gray-300">Stats</span>
        <span className="text-xs text-gray-400">
          Points remaining: <span className={`font-mono ${remaining === 0 ? 'text-green-400' : remaining < 0 ? 'text-red-400' : 'text-purple-400'}`}>{remaining}</span>
        </span>
      </div>

      {STAT_NAMES.map(stat => {
        const value = stats[stat] || 10;
        const diff = value - 10;
        return (
          <div key={stat} className="flex items-center gap-3">
            <span className="w-28 text-sm text-gray-300 capitalize">{stat}</span>
            <button
              onClick={() => handleStatChange(stat, -1)}
              disabled={value <= 1}
              className="w-7 h-7 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-30 disabled:cursor-not-allowed text-gray-200 text-sm font-bold transition-colors flex items-center justify-center"
            >
              -
            </button>
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <div className="flex-1 h-2 bg-gray-700 rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all ${value > 15 ? 'bg-green-500' : value > 10 ? 'bg-blue-500' : value < 8 ? 'bg-red-500' : 'bg-gray-500'}`}
                    style={{ width: `${(value / 30) * 100}%` }}
                  />
                </div>
                <span className="w-7 text-right font-mono text-sm text-gray-200">{value}</span>
                {diff !== 0 && (
                  <span className={`text-xs w-4 text-right ${diff > 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {diff > 0 ? `+${diff}` : diff}
                  </span>
                )}
              </div>
            </div>
            <button
              onClick={() => handleStatChange(stat, 1)}
              disabled={value >= 30 || remaining <= 0}
              className="w-7 h-7 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-30 disabled:cursor-not-allowed text-gray-200 text-sm font-bold transition-colors flex items-center justify-center"
            >
              +
            </button>
          </div>
        );
      })}

      <div className="text-xs text-gray-500 pt-1">
        Base stats are 10. You have {POINT_POOL} points to distribute. Each point above 10 costs 1, each point below 10 refunds 1.
      </div>
    </div>
  );
}

function SkillInput({ skills, onChange }) {
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newRating, setNewRating] = useState(3);
  const [newType, setNewType] = useState('active');
  const [expandedSkill, setExpandedSkill] = useState(null);
  const [editDesc, setEditDesc] = useState('');
  const [editTriggers, setEditTriggers] = useState('');
  const [editType, setEditType] = useState('active');

  const addSkill = () => {
    const name = newName.trim().toLowerCase();
    if (!name) return;
    onChange({
      ...skills,
      [name]: {
        type: newType,
        rating: Math.max(1, Math.min(10, newRating)),
        description: newDesc.trim() || `Proficiency in ${newName.trim()}`,
        trigger_words: [],
      },
    });
    setNewName('');
    setNewDesc('');
    setNewRating(3);
    setNewType('active');
  };

  const openEdit = (name) => {
    const skill = skills[name];
    setExpandedSkill(name);
    setEditDesc(skill.description || '');
    setEditTriggers((skill.trigger_words || []).join(', '));
    setEditType(skill.type || 'active');
  };

  const saveEdit = (name) => {
    const desc = editDesc.trim();
    const triggers = editTriggers
      .split(',')
      .map(w => w.trim().toLowerCase())
      .filter(w => w.length > 0);
    onChange({
      ...skills,
      [name]: {
        ...skills[name],
        type: editType,
        description: desc || skills[name].description,
        trigger_words: triggers.length > 0 ? triggers : skills[name].trigger_words,
      },
    });
    setExpandedSkill(null);
  };

  const removeSkill = (name) => {
    const copy = { ...skills };
    delete copy[name];
    onChange(copy);
  };

  const updateSkillRating = (name, delta) => {
    if (!skills[name]) return;
    const current = skills[name].rating || 1;
    const next = current + delta;
    if (next < 1 || next > 10) return;
    onChange({ ...skills, [name]: { ...skills[name], rating: next } });
  };

  return (
    <div className="space-y-3">
      <span className="text-sm font-medium text-gray-300">Starting Skills</span>

      {Object.keys(skills).length > 0 && (
        <div className="space-y-1.5">
          {Object.entries(skills).map(([name, data]) => (
            <React.Fragment key={name}>
            <div className="flex items-center gap-2 bg-gray-900/50 rounded px-3 py-1.5">
              <span className="flex-1 text-sm text-gray-200 capitalize">{name}</span>
              <span className={`text-[9px] px-1.5 py-0.5 rounded ${data.type === 'curse' ? 'bg-red-500/20 text-red-400' : data.type === 'passive' ? 'bg-blue-500/20 text-blue-400' : 'bg-emerald-500/20 text-emerald-400'}`}>
                {data.type || 'active'}
              </span>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => updateSkillRating(name, -1)}
                  disabled={data.rating <= 1}
                  className="w-5 h-5 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-30 text-xs text-gray-200"
                >
                  -
                </button>
                <span className="w-5 text-center font-mono text-xs text-purple-400">{data.rating}/10</span>
                <button
                  onClick={() => updateSkillRating(name, 1)}
                  disabled={data.rating >= 10}
                  className="w-5 h-5 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-30 text-xs text-gray-200"
                >
                  +
                </button>
              </div>
              <button
                onClick={() => openEdit(name)}
                className="text-xs text-gray-400 hover:text-purple-400 transition-colors"
                title="Edit description & triggers"
              >
                &#9998;
              </button>
              <button
                onClick={() => removeSkill(name)}
                className="text-red-400 hover:text-red-300 text-xs"
              >
                x
              </button>
            </div>
            {expandedSkill === name && (
              <div className="ml-6 bg-gray-900/80 rounded px-3 py-2 space-y-2 border border-gray-700">
                <div>
                  <label className="text-[10px] text-gray-500 uppercase tracking-wider">Type</label>
                  <select
                    value={editType}
                    onChange={(e) => setEditType(e.target.value)}
                    className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 focus:border-purple-500 focus:outline-none mt-0.5"
                  >
                    <option value="active">Active</option>
                    <option value="passive">Passive</option>
                    <option value="curse">Curse</option>
                  </select>
                </div>
                <div>
                  <label className="text-[10px] text-gray-500 uppercase tracking-wider">Description</label>
                  <textarea
                    value={editDesc}
                    onChange={(e) => setEditDesc(e.target.value)}
                    placeholder="What this skill does..."
                    rows={2}
                    className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 focus:border-purple-500 focus:outline-none mt-0.5 resize-none"
                  />
                </div>
                <div>
                  <label className="text-[10px] text-gray-500 uppercase tracking-wider">Trigger Words (comma-separated)</label>
                  <input
                    type="text"
                    value={editTriggers}
                    onChange={(e) => setEditTriggers(e.target.value)}
                    placeholder="shadow, step, vanish"
                    className="w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 focus:border-purple-500 focus:outline-none mt-0.5"
                  />
                </div>
                <div className="flex gap-2 justify-end">
                  <button
                    onClick={() => setExpandedSkill(null)}
                    className="px-2 py-1 text-xs text-gray-400 hover:text-gray-300"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={() => saveEdit(name)}
                    className="px-3 py-1 bg-purple-600 hover:bg-purple-500 rounded text-xs font-medium transition-colors"
                  >
                    Save
                  </button>
                </div>
              </div>
            )}
            </React.Fragment>
          ))}
        </div>
      )}

      <div className="flex gap-2 items-end">
        <div className="flex-1">
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Skill name"
            className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 focus:border-purple-500 focus:outline-none"
          />
        </div>
        <div className="w-14">
          <select
            value={newType}
            onChange={(e) => setNewType(e.target.value)}
            className="w-full bg-gray-900 border border-gray-700 rounded px-1.5 py-1.5 text-xs text-gray-200 focus:border-purple-500 focus:outline-none"
          >
            <option value="active">Act</option>
            <option value="passive">Pas</option>
            <option value="curse">Cur</option>
          </select>
        </div>
        <div className="w-14">
          <input
            type="number"
            value={newRating}
            onChange={(e) => setNewRating(Math.max(1, Math.min(10, parseInt(e.target.value) || 3)))}
            min={1}
            max={10}
            className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 focus:border-purple-500 focus:outline-none text-center"
          />
        </div>
        <button
          onClick={addSkill}
          disabled={!newName.trim()}
          className="px-3 py-1.5 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded text-xs font-medium transition-colors whitespace-nowrap"
        >
          Add
        </button>
      </div>
      <div>
        <input
          type="text"
          value={newDesc}
          onChange={(e) => setNewDesc(e.target.value)}
          placeholder="Short description (optional)"
          className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-xs text-gray-400 focus:border-purple-500 focus:outline-none mt-1"
        />
      </div>
    </div>
  );
}

export default function CharWidget({ value, onChange }) {
  const [stats, setStats] = useState(() => (value && value.stats) ? { ...value.stats } : { ...DEFAULT_STATS });
  const [skills, setSkills] = useState(() => (value && value.skills) ? { ...value.skills } : {});

  useEffect(() => {
    if (value) {
      if (value.stats) setStats({ ...value.stats });
      if (value.skills) setSkills({ ...value.skills });
    }
  }, [value]);

  const emitChange = (newStats, newSkills) => {
    const con = newStats.vitality || 10;
    const maxHp = con * 7 + 2;
    onChange({
      stats: newStats,
      skills: newSkills,
      level: 1,
      xp: 0,
      hp: maxHp,
      max_hp: maxHp,
    });
  };

  const handleStatsChange = (newStats) => {
    setStats(newStats);
    emitChange(newStats, skills);
  };

  const handleSkillsChange = (newSkills) => {
    setSkills(newSkills);
    emitChange(stats, newSkills);
  };

  return (
    <div className="border-t border-gray-700 pt-4 space-y-4">
      <StatAllocation stats={stats} onChange={handleStatsChange} />
      <SkillInput skills={skills} onChange={handleSkillsChange} />
    </div>
  );
}
