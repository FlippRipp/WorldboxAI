import React, { useState, useEffect } from 'react';

const FALLBACK_TIERS = [
  { min: 1, max: 4, label: 'Severely Impaired' },
  { min: 5, max: 8, label: 'Below Average' },
  { min: 9, max: 12, label: 'Average Human' },
  { min: 13, max: 16, label: 'Above Average / Trained' },
  { min: 17, max: 20, label: 'Expert / Peak Human' },
  { min: 21, max: 25, label: 'Superhuman' },
  { min: 26, max: 30, label: 'Legendary / Demigod' },
];

const STAT_DESCRIPTIONS = {
  power: 'Raw physical might. Governs melee attacks, lifting, breaking obstacles, and feats of strength.',
  agility: 'Speed, reflexes, and precision. Governs ranged attacks, stealth, acrobatics, and evasion.',
  vitality: 'Stamina and resilience. Governs HP, endurance, poison resistance, and survival.',
  intelligence: 'Knowledge and reasoning. Governs magic power, investigation, crafting, and languages.',
  spirit: 'Perception and willpower. Governs insight, instincts, mental resistance, and spiritual power.',
  charm: 'Presence and influence. Governs persuasion, deception, leadership, and social power.',
};

function tierFor(val, tiers) {
  for (const t of tiers) {
    if (val >= t.min && val <= t.max) return t.label;
  }
  return 'Unknown';
}

const TYPE_STYLES = {
  active: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  passive: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  curse: 'bg-red-500/20 text-red-400 border-red-500/30',
};

const TYPE_LABELS = {
  active: 'Active',
  passive: 'Passive',
  curse: 'Curse',
};

const API_BASE = '/api/modules/wb_core_rpg';
const NEW_SKILL = '__new__';

function effectDurationLabel(effect, nowMinutes) {
  if (effect.duration_turns != null) {
    return `${effect.duration_turns} turn${effect.duration_turns !== 1 ? 's' : ''}`;
  }
  if (effect.expires_at_minutes != null && nowMinutes != null) {
    const left = Math.max(0, effect.expires_at_minutes - nowMinutes);
    if (left >= 1440) return `~${Math.round(left / 1440)}d`;
    if (left >= 60) return `~${Math.round(left / 60)}h`;
    return `${left}m`;
  }
  return 'ongoing';
}

function StatusEffectList({ effects, nowMinutes, detailed = false }) {
  if (!effects || effects.length === 0) return null;
  return (
    <div className="space-y-1">
      {effects.map((e) => (
        <div
          key={e.name}
          className={`rounded px-2 py-1 border text-xs ${e.kind === 'good' ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-red-500/10 border-red-500/30'}`}
          title={e.description}
        >
          <div className="flex items-center justify-between">
            <span className={`capitalize truncate ${e.kind === 'good' ? 'text-emerald-300' : 'text-red-300'}`}>{e.name}</span>
            <span className="text-gray-500 font-mono ml-2 shrink-0">{effectDurationLabel(e, nowMinutes)}</span>
          </div>
          {detailed && e.description && (
            <div className="text-[10px] text-gray-500 mt-0.5 leading-tight">{e.description}</div>
          )}
        </div>
      ))}
    </div>
  );
}

function xpForLevel(level, steepness = 2) {
  return Math.floor(50 * Math.pow(level, steepness));
}

function totalXpForLevel(level, steepness = 2) {
  let total = 0;
  for (let n = 1; n < level; n++) total += xpForLevel(n, steepness);
  return total;
}

function SkillEditForm({ form, setForm, saving, saveError, confirmingDelete, onSave, onCancel, onDelete }) {
  const set = (field) => (e) => setForm((prev) => ({ ...prev, [field]: e.target.value }));
  return (
    <div className="bg-gray-800/60 rounded-lg border border-indigo-500/40 p-3 space-y-2">
      <div className="flex gap-2">
        <input
          value={form.name}
          onChange={set('name')}
          placeholder="Skill name"
          className="flex-1 min-w-0 bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
        />
        <select
          value={form.type}
          onChange={set('type')}
          className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none"
        >
          <option value="active">Active</option>
          <option value="passive">Passive</option>
          <option value="curse">Curse</option>
        </select>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-xs text-gray-500">Rating</span>
        <input
          type="range"
          min="1"
          max="10"
          value={form.rating}
          onChange={set('rating')}
          className="flex-1 accent-purple-500"
        />
        <span className="text-purple-400 font-mono text-sm w-10 text-right">{form.rating}/10</span>
      </div>
      <textarea
        value={form.description}
        onChange={set('description')}
        placeholder="What the skill does, how it manifests, its limits…"
        rows={3}
        className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300 leading-relaxed resize-y focus:border-indigo-500 focus:outline-none"
      />
      <input
        value={form.trigger_words}
        onChange={set('trigger_words')}
        placeholder="Trigger words (comma-separated)"
        className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300 focus:border-indigo-500 focus:outline-none"
      />
      {saveError && <div className="text-xs text-red-400">{saveError}</div>}
      <div className="flex items-center gap-2 pt-1">
        <button
          onClick={onSave}
          disabled={saving}
          className="px-3 py-1 text-xs text-indigo-200 bg-indigo-600/60 hover:bg-indigo-600/80 disabled:opacity-50 border border-indigo-500/50 rounded transition-colors"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          onClick={onCancel}
          disabled={saving}
          className="px-3 py-1 text-xs text-gray-400 hover:text-gray-200 bg-gray-800 border border-gray-700 rounded transition-colors"
        >
          Cancel
        </button>
        {onDelete && (
          <button
            onClick={onDelete}
            disabled={saving}
            className="ml-auto px-3 py-1 text-xs text-red-300 bg-red-900/30 hover:bg-red-900/50 disabled:opacity-50 border border-red-800/50 rounded transition-colors"
          >
            {confirmingDelete ? 'Confirm delete?' : 'Delete'}
          </button>
        )}
      </div>
    </div>
  );
}

function statBarColor(val, max) {
  const pct = val / max;
  if (pct >= 0.8) return 'bg-indigo-500';
  if (pct >= 0.6) return 'bg-blue-500';
  if (pct >= 0.4) return 'bg-cyan-500';
  return 'bg-slate-500';
}

// Custom keyframes live in a module-local <style> tag: Tailwind scans module
// JSX for utility classes but cannot synthesize keyframes from here. Names
// are wbrpg-prefixed to avoid global collisions.
const WBRPG_CSS = `
@keyframes wbrpg-burst { 0% { transform: scale(0.5); opacity: 0; } 60% { transform: scale(1.1); opacity: 1; } 100% { transform: scale(1); opacity: 1; } }
@keyframes wbrpg-glow { 0%, 100% { box-shadow: 0 0 14px 2px rgba(168, 85, 247, 0.35); } 50% { box-shadow: 0 0 42px 12px rgba(168, 85, 247, 0.75); } }
@keyframes wbrpg-rise { 0% { transform: translateY(0) scale(1); opacity: 0; } 15% { opacity: 1; } 100% { transform: translateY(-110px) scale(0.3); opacity: 0; } }
@keyframes wbrpg-shimmer { 0%, 100% { opacity: 0.45; } 50% { opacity: 1; } }
@keyframes wbrpg-dissolve { 0% { opacity: 1; filter: blur(0); } 100% { opacity: 0.15; filter: blur(5px); transform: translateY(-6px); } }
.wbrpg-burst { animation: wbrpg-burst 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) both; }
.wbrpg-glow { animation: wbrpg-glow 1.2s ease-in-out infinite; }
.wbrpg-rise { animation: wbrpg-rise 1.8s ease-out infinite; }
.wbrpg-shimmer { animation: wbrpg-shimmer 1.4s ease-in-out infinite; }
.wbrpg-dissolve { animation: wbrpg-dissolve 2s ease-in forwards; }
`;

function TierChip({ data }) {
  const tier = data?.tier ?? 1;
  if (tier <= 1) return null;
  return (
    <span
      className="text-[9px] px-1 py-px rounded border bg-amber-500/20 text-amber-400 border-amber-500/30 font-semibold"
      title={data.evolution_theme ? `Tier ${tier} evolution — ${data.evolution_theme} path` : `Tier ${tier} evolution`}
    >
      T{tier}
    </span>
  );
}

function EvolutionParticles() {
  return (
    <div className="absolute inset-0 overflow-hidden pointer-events-none rounded-xl">
      {[8, 22, 37, 52, 66, 81, 93].map((left, i) => (
        <span
          key={i}
          className="wbrpg-rise absolute bottom-1 w-1.5 h-1.5 rounded-full bg-purple-400"
          style={{ left: `${left}%`, animationDelay: `${i * 0.26}s` }}
        />
      ))}
    </div>
  );
}

function LevelUpModal({ rpg, config, generating, onClose, onApplied }) {
  const stats = rpg.stats || {};
  const skills = rpg.skills || {};
  const attrPts = rpg.unspent_attribute_points ?? 0;
  const skillPts = rpg.unspent_skill_points ?? 0;
  const maxStat = config?.max_stat_value ?? 20;
  const newSkillCost = config?.new_skill_cost ?? 3;

  const [pendingStats, setPendingStats] = useState({});
  const [pendingSkills, setPendingSkills] = useState({});
  const [newSkill, setNewSkill] = useState(null); // {name, type, description}
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') onClose(); }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const statSpent = Object.values(pendingStats).reduce((a, b) => a + b, 0);
  const skillSpent = Object.values(pendingSkills).reduce((a, b) => a + b, 0) + (newSkill ? newSkillCost : 0);
  const attrLeft = attrPts - statSpent;
  const skillLeft = skillPts - skillSpent;

  const bump = (setter) => (key, delta) =>
    setter((prev) => {
      const next = { ...prev };
      const v = (next[key] || 0) + delta;
      if (v <= 0) delete next[key];
      else next[key] = v;
      return next;
    });
  const bumpStat = bump(setPendingStats);
  const bumpSkill = bump(setPendingSkills);

  const raisableSkills = Object.entries(skills).filter(([, d]) => d.type !== 'curse');
  const anythingSpent = statSpent > 0 || skillSpent > 0;
  const canConfirm = anythingSpent && !saving && !generating && attrLeft >= 0 && skillLeft >= 0
    && (!newSkill || newSkill.name.trim());

  async function confirm() {
    setSaving(true);
    setError('');
    try {
      const payload = {};
      if (statSpent > 0) payload.stat_allocations = pendingStats;
      if (Object.keys(pendingSkills).length > 0) payload.skill_allocations = pendingSkills;
      if (newSkill) {
        payload.new_skill = {
          name: newSkill.name.trim(),
          type: newSkill.type || 'active',
          description: newSkill.description || '',
        };
      }
      const res = await fetch(`${API_BASE}/levelup/spend`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Save failed (HTTP ${res.status})`);
      onApplied(data);
    } catch (e) {
      setError(e.message);
      setSaving(false);
    }
  }

  const plusMinus = (onMinus, onPlus, minusDisabled, plusDisabled) => (
    <span className="flex items-center gap-1">
      <button
        onClick={onMinus}
        disabled={minusDisabled}
        className="w-6 h-6 flex items-center justify-center text-sm rounded bg-gray-800 border border-gray-700 text-gray-300 hover:bg-gray-700 disabled:opacity-30 transition-colors"
      >
        {'−'}
      </button>
      <button
        onClick={onPlus}
        disabled={plusDisabled}
        className="w-6 h-6 flex items-center justify-center text-sm rounded bg-indigo-600/50 border border-indigo-500/50 text-indigo-200 hover:bg-indigo-600/80 disabled:opacity-30 transition-colors"
      >
        +
      </button>
    </span>
  );

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.75)' }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <style>{WBRPG_CSS}</style>
      <div
        className="bg-gray-900 border border-amber-500/40 rounded-xl w-full max-w-lg max-h-[85vh] overflow-y-auto shadow-2xl wbrpg-burst"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 pt-6 pb-4 text-center border-b border-gray-800">
          <div className="text-3xl font-extrabold tracking-wide text-amber-300 wbrpg-burst">{'⭐'} LEVEL UP!</div>
          <div className="text-sm text-gray-300 mt-1">You reached <span className="text-amber-300 font-semibold">Level {rpg.level}</span></div>
          <div className="flex items-center justify-center gap-2 mt-3 text-xs">
            <span className="px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 font-mono">
              {attrLeft} attribute point{attrLeft === 1 ? '' : 's'}
            </span>
            <span className="px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-300 border border-purple-500/30 font-mono">
              {skillLeft} skill point{skillLeft === 1 ? '' : 's'}
            </span>
          </div>
        </div>

        <div className="p-5 space-y-5">
          {attrPts > 0 && (
            <section>
              <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Attributes</h3>
              <div className="space-y-1.5">
                {Object.entries(stats).map(([stat, value]) => {
                  const added = pendingStats[stat] || 0;
                  return (
                    <div key={stat} className="flex items-center justify-between text-sm bg-gray-800/40 rounded px-3 py-1.5 border border-gray-700/50">
                      <span className="text-gray-300 capitalize">{stat}</span>
                      <span className="flex items-center gap-3">
                        <span className="font-mono text-gray-200">
                          {value}
                          {added > 0 && <span className="text-emerald-400"> +{added}</span>}
                        </span>
                        {plusMinus(
                          () => bumpStat(stat, -1),
                          () => bumpStat(stat, 1),
                          added === 0 || saving,
                          attrLeft <= 0 || value + added >= maxStat || saving,
                        )}
                      </span>
                    </div>
                  );
                })}
              </div>
            </section>
          )}

          {skillPts > 0 && (
            <section>
              <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Skills</h3>
              <div className="space-y-1.5">
                {raisableSkills.map(([name, data]) => {
                  const added = pendingSkills[name] || 0;
                  return (
                    <div key={name} className="flex items-center justify-between text-sm bg-gray-800/40 rounded px-3 py-1.5 border border-gray-700/50">
                      <span className="text-gray-300 capitalize flex items-center gap-1.5 truncate">
                        {name} <TierChip data={data} />
                      </span>
                      <span className="flex items-center gap-3">
                        <span className="font-mono text-gray-200">
                          {data.rating}
                          {added > 0 && <span className="text-emerald-400"> +{added}</span>}
                          <span className="text-gray-500">/10</span>
                        </span>
                        {plusMinus(
                          () => bumpSkill(name, -1),
                          () => bumpSkill(name, 1),
                          added === 0 || saving,
                          skillLeft <= 0 || data.rating + added >= 10 || saving,
                        )}
                      </span>
                    </div>
                  );
                })}

                {newSkill ? (
                  <div className="bg-gray-800/60 rounded-lg border border-purple-500/40 p-3 space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-purple-300 font-semibold">New skill {'—'} {newSkillCost} point{newSkillCost === 1 ? '' : 's'}</span>
                      <button onClick={() => setNewSkill(null)} className="text-gray-500 hover:text-gray-200 text-sm leading-none">{'✕'}</button>
                    </div>
                    <div className="flex gap-2">
                      <input
                        value={newSkill.name}
                        onChange={(e) => setNewSkill({ ...newSkill, name: e.target.value })}
                        placeholder="Skill name"
                        className="flex-1 min-w-0 bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200 focus:border-purple-500 focus:outline-none"
                      />
                      <select
                        value={newSkill.type}
                        onChange={(e) => setNewSkill({ ...newSkill, type: e.target.value })}
                        className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-sm text-gray-200 focus:border-purple-500 focus:outline-none"
                      >
                        <option value="active">Active</option>
                        <option value="passive">Passive</option>
                      </select>
                    </div>
                    <textarea
                      value={newSkill.description}
                      onChange={(e) => setNewSkill({ ...newSkill, description: e.target.value })}
                      placeholder="What the skill does, how it manifests, its limits…"
                      rows={2}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-gray-300 leading-relaxed resize-y focus:border-purple-500 focus:outline-none"
                    />
                    <div className="text-[10px] text-gray-500">Starts at rating {Math.min(10, newSkillCost)}.</div>
                  </div>
                ) : (
                  <button
                    onClick={() => setNewSkill({ name: '', type: 'active', description: '' })}
                    disabled={skillLeft < newSkillCost || saving}
                    className="w-full py-1.5 px-3 text-xs text-purple-300 bg-purple-500/10 hover:bg-purple-500/20 disabled:opacity-40 border border-dashed border-purple-500/40 rounded-lg transition-colors"
                  >
                    + Learn a new skill ({newSkillCost} pt{newSkillCost === 1 ? '' : 's'})
                  </button>
                )}
              </div>
            </section>
          )}

          {error && <div className="text-xs text-red-400">{error}</div>}
          {generating && (
            <div className="text-xs text-amber-400/80">Waiting for the current turn to finish{'…'}</div>
          )}

          <div className="flex items-center gap-2 pt-1">
            <button
              onClick={confirm}
              disabled={!canConfirm}
              className="flex-1 py-2 text-sm font-semibold text-amber-100 bg-amber-600/70 hover:bg-amber-600/90 disabled:opacity-40 border border-amber-500/50 rounded-lg transition-colors"
            >
              {saving ? 'Applying…' : 'Confirm'}
            </button>
            <button
              onClick={onClose}
              disabled={saving}
              className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200 bg-gray-800 border border-gray-700 rounded-lg transition-colors"
            >
              Save for later
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function SkillEvolutionModal({ skillName, rpg, generating, onDeferred, onEvolved }) {
  const skill = (rpg.skills || {})[skillName] || {};
  const tier = skill.tier ?? 1;

  const [phase, setPhase] = useState('loading'); // loading | choose | evolving | reveal
  const [options, setOptions] = useState(null);
  const [error, setError] = useState('');
  const [chosenTheme, setChosenTheme] = useState(null);
  const [result, setResult] = useState(null); // {rpg, evolved}
  const [deferring, setDeferring] = useState(false);

  async function loadOptions() {
    setPhase('loading');
    setError('');
    try {
      const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(skillName)}/evolution-options`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Failed to prepare options (HTTP ${res.status})`);
      setOptions(data.options || []);
      setPhase('choose');
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => { loadOptions(); }, [skillName]);

  async function choose(theme) {
    setChosenTheme(theme);
    setPhase('evolving');
    setError('');
    // The evolve request runs WHILE the animation plays; the reveal waits for
    // both so the animation never gets cut short.
    const minDelay = new Promise((resolve) => setTimeout(resolve, 2500));
    try {
      const request = fetch(`${API_BASE}/skills/${encodeURIComponent(skillName)}/evolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme }),
      }).then(async (res) => {
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `Evolution failed (HTTP ${res.status})`);
        return data;
      });
      const [data] = await Promise.all([request, minDelay]);
      setResult(data);
      setPhase('reveal');
    } catch (e) {
      setError(e.message);
      setPhase('choose');
    }
  }

  async function defer() {
    if (deferring) return;
    setDeferring(true);
    try {
      const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(skillName)}/evolution`, { method: 'DELETE' });
      const data = await res.json().catch(() => ({}));
      onDeferred(res.ok ? data : null);
    } catch {
      onDeferred(null);
    }
  }

  useEffect(() => {
    function onKey(e) {
      if (e.key !== 'Escape') return;
      if (phase === 'reveal' && result) onEvolved(result.rpg);
      else if (phase !== 'evolving') defer();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [phase, result]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}
    >
      <style>{WBRPG_CSS}</style>
      <div
        className={`relative bg-gray-900 border rounded-xl w-full max-w-md shadow-2xl wbrpg-burst ${phase === 'evolving' ? 'border-purple-500/70 wbrpg-glow' : 'border-purple-500/40'}`}
        onClick={(e) => e.stopPropagation()}
      >
        {phase === 'evolving' && <EvolutionParticles />}

        <div className="px-5 pt-6 pb-4 text-center">
          <div className="text-2xl font-extrabold tracking-wide text-purple-300">{'✦'} LEVEL UP {'✦'}</div>
          <div className="text-sm text-gray-300 mt-1">
            <span className="capitalize font-semibold text-gray-100">{skillName}</span> has reached its peak
            {tier > 1 ? ` (Tier ${tier})` : ''} and can evolve.
          </div>
        </div>

        <div className="px-5 pb-6 space-y-4">
          {phase === 'loading' && !error && (
            <div className="text-center py-6">
              <div className="text-sm text-purple-300 wbrpg-shimmer">Preparing skill progression options{'…'}</div>
              <div className="text-[11px] text-gray-500 mt-2">The fates are weighing where this power could go.</div>
            </div>
          )}

          {phase === 'loading' && error && (
            <div className="text-center py-4 space-y-3">
              <div className="text-xs text-red-400">{error}</div>
              <button
                onClick={loadOptions}
                className="px-4 py-1.5 text-xs text-purple-200 bg-purple-600/50 hover:bg-purple-600/70 border border-purple-500/50 rounded transition-colors"
              >
                Try again
              </button>
            </div>
          )}

          {phase === 'choose' && (
            <>
              <div className="text-xs text-gray-400 text-center">Choose the path this skill will take:</div>
              <div className="space-y-2">
                {(options || []).map((opt) => (
                  <button
                    key={opt.theme}
                    onClick={() => choose(opt.theme)}
                    disabled={generating}
                    className="w-full text-left px-4 py-3 rounded-lg bg-gray-800/60 border border-gray-700 hover:border-purple-500/60 hover:bg-purple-500/10 disabled:opacity-50 transition-colors group"
                  >
                    <div className="text-sm font-bold text-purple-300 group-hover:text-purple-200">{opt.theme}</div>
                    {opt.summary && <div className="text-[11px] text-gray-500 mt-0.5">{opt.summary}</div>}
                  </button>
                ))}
              </div>
              {error && <div className="text-xs text-red-400 text-center">{error}</div>}
              {generating && <div className="text-xs text-amber-400/80 text-center">Waiting for the current turn to finish{'…'}</div>}
              <button
                onClick={defer}
                disabled={deferring}
                className="w-full py-1.5 text-xs text-gray-500 hover:text-gray-300 transition-colors"
              >
                {deferring ? 'Saving…' : 'Decide later'}
              </button>
            </>
          )}

          {phase === 'evolving' && (
            <div className="text-center py-8 space-y-3">
              <div className="text-lg font-bold text-gray-200 capitalize wbrpg-dissolve">{skillName}</div>
              <div className="text-sm text-purple-300 wbrpg-shimmer">
                Evolving down the <span className="font-semibold">{chosenTheme}</span> path{'…'}
              </div>
            </div>
          )}

          {phase === 'reveal' && result && (
            <div className="text-center py-4 space-y-3">
              <div className="text-xs text-gray-500 capitalize line-through">{result.evolved.old_name}</div>
              <div className="text-xl font-extrabold text-amber-300 capitalize wbrpg-burst">{result.evolved.new_name}</div>
              <div className="text-xs text-purple-300 font-semibold">
                Tier {result.evolved.tier} {'•'} {result.evolved.theme} path
              </div>
              {result.evolved.description && (
                <div className="text-[11px] text-gray-400 leading-relaxed px-2">{result.evolved.description}</div>
              )}
              <button
                onClick={() => onEvolved(result.rpg)}
                className="mt-2 px-6 py-2 text-sm font-semibold text-purple-100 bg-purple-600/70 hover:bg-purple-600/90 border border-purple-500/50 rounded-lg transition-colors"
              >
                Continue
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function CoreRpgWidget({ state, config, generating }) {
  const [showStats, setShowStats] = useState(true);
  const [showSkills, setShowSkills] = useState(true);
  const [showFullSheet, setShowFullSheet] = useState(false);

  // Skill editing (full sheet). Edits are saved through the module's API and
  // mirrored locally until the next server state refresh carries them back.
  const [skillsOverride, setSkillsOverride] = useState(null);
  const [editingSkill, setEditingSkill] = useState(null); // skill key or NEW_SKILL
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  // Level-up popup + skill evolution flow. API responses are mirrored into
  // rpgOverride until the next server state refresh carries them back.
  const [rpgOverride, setRpgOverride] = useState(null);
  const [showLevelUp, setShowLevelUp] = useState(false);
  const [evolvingSkill, setEvolvingSkill] = useState(null); // skill key
  const prevLevelRef = React.useRef(null);

  const serverRpg = state?.module_data?.wb_core_rpg;
  useEffect(() => { setSkillsOverride(null); setRpgOverride(null); }, [serverRpg]);

  useEffect(() => {
    if (!showFullSheet) return;
    function onKey(e) {
      if (e.key !== 'Escape') return;
      if (editingSkill !== null) { setEditingSkill(null); setForm(null); }
      else setShowFullSheet(false);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [showFullSheet, editingSkill]);

  const rpg = rpgOverride ?? serverRpg;

  const liveLevel = rpg?.level ?? 1;
  const attrPts = rpg?.unspent_attribute_points ?? 0;
  const skillPts = rpg?.unspent_skill_points ?? 0;
  const pendingEvos = (rpg?.pending_evolutions ?? []).filter((e) => e && e.skill);
  const nextPendingEvo = pendingEvos.find((e) => e.status === 'pending');
  const pendingEvoSkills = new Set(pendingEvos.map((e) => e.skill));

  // Auto-open the level-up popup on a live level increase (server state
  // arrives over the websocket after the turn). Banked points also keep a
  // persistent banner visible, so a reload never loses the reward.
  useEffect(() => {
    if (!rpg) return;
    if (prevLevelRef.current !== null && liveLevel > prevLevelRef.current && attrPts + skillPts > 0) {
      setShowLevelUp(true);
    }
    prevLevelRef.current = liveLevel;
  }, [liveLevel, rpg ? 1 : 0]);

  // Auto-open the evolution flow for the first pending skill once nothing
  // else is on screen; deferred entries only reopen via their Evolve badge.
  useEffect(() => {
    if (!nextPendingEvo || generating || showLevelUp || showFullSheet || evolvingSkill) return;
    setEvolvingSkill(nextPendingEvo.skill);
  }, [nextPendingEvo?.skill, generating, showLevelUp, showFullSheet, evolvingSkill]);

  if (!rpg) return null;

  function startEdit(name, data) {
    setEditingSkill(name);
    setForm({
      name,
      rating: data.rating ?? 1,
      description: data.description || '',
      trigger_words: (data.trigger_words || []).join(', '),
      type: data.type || 'active',
    });
    setSaveError('');
    setConfirmingDelete(false);
  }

  function startAdd() {
    setEditingSkill(NEW_SKILL);
    setForm({ name: '', rating: 3, description: '', trigger_words: '', type: 'active' });
    setSaveError('');
    setConfirmingDelete(false);
  }

  function cancelEdit() {
    setEditingSkill(null);
    setForm(null);
    setSaveError('');
  }

  async function saveSkill() {
    const isNew = editingSkill === NEW_SKILL;
    const payload = {
      name: form.name.trim(),
      rating: Number(form.rating),
      description: form.description,
      trigger_words: form.trigger_words.split(',').map((w) => w.trim()).filter(Boolean),
      type: form.type,
    };
    if (!payload.name) { setSaveError('Skill name cannot be empty.'); return; }
    setSaving(true);
    setSaveError('');
    try {
      const url = isNew
        ? `${API_BASE}/skills`
        : `${API_BASE}/skills/${encodeURIComponent(editingSkill)}`;
      const res = await fetch(url, {
        method: isNew ? 'POST' : 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Save failed (HTTP ${res.status})`);
      setSkillsOverride({ ...(data.skills || {}) });
      cancelEdit();
    } catch (e) {
      setSaveError(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function deleteSkill() {
    if (!confirmingDelete) { setConfirmingDelete(true); return; }
    setSaving(true);
    setSaveError('');
    try {
      const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(editingSkill)}`, { method: 'DELETE' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Delete failed (HTTP ${res.status})`);
      setSkillsOverride({ ...(data.skills || {}) });
      cancelEdit();
    } catch (e) {
      setSaveError(e.message);
    } finally {
      setSaving(false);
      setConfirmingDelete(false);
    }
  }

  const hp = rpg.hp ?? 0;
  const maxHp = rpg.max_hp ?? 100;
  const level = rpg.level ?? 1;
  const xp = rpg.xp ?? 0;
  const stats = rpg.stats ?? {};
  const skills = skillsOverride ?? rpg.skills ?? {};
  const tiers = rpg.stat_tiers ?? FALLBACK_TIERS;
  const backstory = rpg.backstory ?? '';
  const unconscious = hp <= 0;
  const statusEffects = Array.isArray(rpg.status_effects) ? rpg.status_effects : [];
  const nowMinutes = state?.module_data?.wb_time_tracker?.clock?.total_minutes_elapsed ?? null;

  const hpPct = maxHp > 0 ? Math.max(0, Math.min(100, (hp / maxHp) * 100)) : 100;
  const hpColor =
    hpPct > 60 ? 'bg-green-500' :
    hpPct > 30 ? 'bg-yellow-500' :
    'bg-red-500';

  const steepness = config?.xp_curve_steepness ?? 2;
  const xpNeeded = totalXpForLevel(level + 1, steepness);
  const xpCurrent = totalXpForLevel(level, steepness);
  const xpIntoLevel = xp - xpCurrent;
  const xpForThisLevel = xpNeeded - xpCurrent;
  const xpPct = xpForThisLevel > 0 ? Math.min(100, (xpIntoLevel / xpForThisLevel) * 100) : 100;

  const maxStatVal = Math.max(20, ...Object.values(stats));
  const skillEntries = Object.entries(skills).sort((a, b) => b[1].rating - a[1].rating);

  return (
    <>
      <div className="bg-gray-900/70 rounded-lg border border-gray-700 p-3 space-y-3 text-sm">
        <div className="flex items-center justify-between">
          <span className="text-gray-300 font-semibold">Level {level}</span>
          {unconscious && (
            <span className="text-red-400 text-xs animate-pulse">UNCONSCIOUS</span>
          )}
        </div>

        {attrPts + skillPts > 0 && (
          <button
            onClick={() => setShowLevelUp(true)}
            className="w-full py-1.5 px-3 text-xs font-semibold text-amber-300 bg-amber-500/15 hover:bg-amber-500/25 border border-amber-500/40 rounded animate-pulse transition-colors"
          >
            {'⭐'} Level Up! {attrPts + skillPts} point{attrPts + skillPts === 1 ? '' : 's'} to spend
          </button>
        )}

        <div>
          <div className="flex justify-between text-xs mb-1">
            <span className="text-gray-400">HP</span>
            <span className="text-gray-200 font-mono">{hp}/{maxHp}</span>
          </div>
          <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden">
            <div
              className={`h-full ${hpColor} transition-all duration-500 rounded-full`}
              style={{ width: `${hpPct}%` }}
            />
          </div>
        </div>

        <div>
          <div className="flex justify-between text-xs mb-1">
            <span className="text-gray-400">XP</span>
            <span className="text-gray-200 font-mono">{xpIntoLevel}/{xpForThisLevel}</span>
          </div>
          <div className="w-full h-1.5 bg-gray-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-amber-500 transition-all duration-500 rounded-full"
              style={{ width: `${xpPct}%` }}
            />
          </div>
        </div>

        {statusEffects.length > 0 && (
          <div>
            <div className="text-xs text-gray-400 uppercase tracking-wider mb-1">Effects</div>
            <StatusEffectList effects={statusEffects} nowMinutes={nowMinutes} />
          </div>
        )}

        <button
          onClick={() => setShowStats(!showStats)}
          className="w-full flex items-center justify-between text-xs text-gray-400 hover:text-gray-200 transition-colors"
        >
          <span className="uppercase tracking-wider">Stats</span>
          <span className="text-gray-500">{showStats ? '\u25BC' : '\u25B6'}</span>
        </button>

        {showStats && (
          <div className="space-y-1.5">
            {Object.entries(stats).map(([stat, value]) => (
              <div key={stat}>
                <div className="flex items-center justify-between text-xs">
                  <span className="text-gray-300 capitalize">{stat}</span>
                  <span className="text-gray-500 font-mono">{value}</span>
                </div>
                <div className="w-full h-1.5 bg-gray-700/50 rounded-full overflow-hidden mt-0.5">
                  <div
                    className={`h-full rounded-full transition-all ${statBarColor(value, maxStatVal)}`}
                    style={{ width: `${Math.min(100, (value / maxStatVal) * 100)}%` }}
                  />
                </div>
                <div className="text-[10px] text-gray-600">{tierFor(value, tiers)}</div>
              </div>
            ))}
          </div>
        )}

        {skillEntries.length > 0 && (
          <>
            <button
              onClick={() => setShowSkills(!showSkills)}
              className="w-full flex items-center justify-between text-xs text-gray-400 hover:text-gray-200 transition-colors"
            >
              <span className="uppercase tracking-wider">Skills ({skillEntries.length})</span>
              <span className="text-gray-500">{showSkills ? '\u25BC' : '\u25B6'}</span>
            </button>

            {showSkills && (
              <div className="space-y-1.5">
                {skillEntries.map(([name, data]) => (
                  <div key={name}>
                    <div className="flex items-center justify-between text-xs">
                      <span className="text-gray-300 truncate flex items-center gap-1.5">
                        <span className="capitalize">{name}</span>
                        <TierChip data={data} />
                        <span className={`text-[9px] px-1 py-px rounded border ${TYPE_STYLES[data.type] || TYPE_STYLES.active}`}>{TYPE_LABELS[data.type] || TYPE_LABELS.active}</span>
                      </span>
                      <span className="flex items-center gap-1.5 ml-2">
                        {pendingEvoSkills.has(name) && (
                          <button
                            onClick={() => setEvolvingSkill(name)}
                            className="text-[9px] px-1.5 py-px rounded border bg-amber-500/20 text-amber-300 border-amber-500/40 hover:bg-amber-500/35 animate-pulse transition-colors"
                            title="This skill is ready to evolve"
                          >
                            {'⬆'} Evolve
                          </button>
                        )}
                        <span className="text-purple-400 font-mono">{data.rating}/10</span>
                      </span>
                    </div>
                    <div className="w-full h-1 bg-gray-700/50 rounded-full overflow-hidden mt-0.5">
                      <div
                        className="h-full bg-purple-500 rounded-full transition-all"
                        style={{ width: `${(data.rating / 10) * 100}%` }}
                      />
                    </div>
                    {data.description && (
                      <div className="text-[10px] text-gray-600 mt-0.5 leading-tight">{data.description}</div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        <button
          onClick={() => setShowFullSheet(true)}
          className="w-full py-1.5 px-3 text-xs text-indigo-400 bg-indigo-500/10 hover:bg-indigo-500/20 border border-indigo-500/30 rounded transition-colors"
        >
          View Full Character Sheet
        </button>
      </div>

      {showFullSheet && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ backgroundColor: 'rgba(0,0,0,0.7)' }}
          onClick={(e) => { if (e.target === e.currentTarget) setShowFullSheet(false); }}
        >
          <div
            className="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-xl max-h-[85vh] overflow-y-auto shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="sticky top-0 z-10 bg-gray-900/95 backdrop-blur border-b border-gray-700 px-5 py-3 flex items-center justify-between rounded-t-xl">
              <div className="flex items-center gap-3">
                <h2 className="text-gray-100 font-bold text-base">Character Sheet</h2>
                <span className="text-xs px-2 py-0.5 bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 rounded-full font-mono">
                  Level {level}
                </span>
                {attrPts + skillPts > 0 && (
                  <button
                    onClick={() => setShowLevelUp(true)}
                    className="text-xs px-2 py-0.5 bg-amber-500/20 text-amber-300 border border-amber-500/40 rounded-full hover:bg-amber-500/30 transition-colors"
                  >
                    {attrPts + skillPts} unspent point{attrPts + skillPts === 1 ? '' : 's'}
                  </button>
                )}
              </div>
              <button
                onClick={() => setShowFullSheet(false)}
                className="text-gray-500 hover:text-gray-200 text-lg leading-none p-1 transition-colors"
              >
                {'\u2715'}
              </button>
            </div>

            <div className="p-5 space-y-5">
              {/* --- Backstory --- */}
              {backstory && (
                <section>
                  <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Backstory</h3>
                  <p className="text-sm text-gray-300 leading-relaxed bg-gray-800/40 rounded-lg p-3 border border-gray-700/50">{backstory}</p>
                </section>
              )}

              {/* --- Vitals --- */}
              <section>
                <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-3">Vitals</h3>
                <div className="space-y-3">
                  <div>
                    <div className="flex justify-between text-xs mb-1">
                      <span className="text-gray-400">HP</span>
                      <span className={`font-mono ${unconscious ? 'text-red-400' : 'text-gray-200'}`}>
                        {hp}/{maxHp}
                      </span>
                    </div>
                    <div className="w-full h-3 bg-gray-800 rounded-full overflow-hidden">
                      <div
                        className={`h-full ${hpColor} transition-all duration-500 rounded-full`}
                        style={{ width: `${hpPct}%` }}
                      />
                    </div>
                  </div>
                  <div>
                    <div className="flex justify-between text-xs mb-1">
                      <span className="text-gray-400">XP</span>
                      <span className="text-gray-200 font-mono">
                        {xpIntoLevel}/{xpForThisLevel} ({xpPct.toFixed(0)}%)
                      </span>
                    </div>
                    <div className="w-full h-2 bg-gray-800 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-amber-500 transition-all duration-500 rounded-full"
                        style={{ width: `${xpPct}%` }}
                      />
                    </div>
                    <div className="text-[10px] text-gray-600 mt-1">Total XP: {xp}</div>
                  </div>
                </div>
              </section>

              {/* --- Status Effects --- */}
              {statusEffects.length > 0 && (
                <section>
                  <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-3">
                    Status Effects ({statusEffects.length})
                  </h3>
                  <StatusEffectList effects={statusEffects} nowMinutes={nowMinutes} detailed />
                </section>
              )}

              {/* --- Attributes --- */}
              <section>
                <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-3">Attributes</h3>
                <div className="grid grid-cols-2 gap-3">
                  {Object.entries(stats).map(([stat, value]) => {
                    const tierLabel = tierFor(value, tiers);
                    const desc = STAT_DESCRIPTIONS[stat] || '';
                    return (
                      <div key={stat} className="bg-gray-800/50 rounded-lg border border-gray-700/50 p-3">
                        <div className="flex items-center justify-between mb-1">
                          <span className="text-gray-200 font-medium capitalize text-sm">{stat}</span>
                          <span className="text-indigo-400 font-mono font-bold text-sm">{value}</span>
                        </div>
                        <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden mb-1.5">
                          <div
                            className={`h-full rounded-full transition-all ${statBarColor(value, maxStatVal)}`}
                            style={{ width: `${Math.min(100, (value / maxStatVal) * 100)}%` }}
                          />
                        </div>
                        <div className="text-[11px] text-amber-400/80 mb-1">{tierLabel}</div>
                        <div className="text-[10px] text-gray-600 leading-tight">{desc}</div>
                      </div>
                    );
                  })}
                </div>
              </section>

              {/* --- Skills --- */}
              <section>
                <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-3">
                  Skills ({skillEntries.length})
                </h3>
                <div className="space-y-2">
                  {skillEntries.length === 0 && editingSkill !== NEW_SKILL && (
                    <div className="text-xs text-gray-600 italic">No skills yet.</div>
                  )}
                  {skillEntries.map(([name, data]) => (
                    editingSkill === name ? (
                      <SkillEditForm
                        key={name}
                        form={form}
                        setForm={setForm}
                        saving={saving}
                        saveError={saveError}
                        confirmingDelete={confirmingDelete}
                        onSave={saveSkill}
                        onCancel={cancelEdit}
                        onDelete={deleteSkill}
                      />
                    ) : (
                      <div key={name} className="group bg-gray-800/50 rounded-lg border border-gray-700/50 p-3">
                        <div className="flex items-center justify-between mb-1">
                          <span className="text-gray-200 font-medium flex items-center gap-1.5 text-sm">
                            <span className="capitalize">{name}</span>
                            <TierChip data={data} />
                            <span className={`text-[10px] px-1.5 py-0.5 rounded border ${TYPE_STYLES[data.type] || TYPE_STYLES.active}`}>{TYPE_LABELS[data.type] || TYPE_LABELS.active}</span>
                          </span>
                          <span className="flex items-center gap-2">
                            {pendingEvoSkills.has(name) && (
                              <button
                                onClick={() => setEvolvingSkill(name)}
                                className="text-[10px] px-1.5 py-0.5 rounded border bg-amber-500/20 text-amber-300 border-amber-500/40 hover:bg-amber-500/35 transition-colors"
                              >
                                {'⬆'} Evolve
                              </button>
                            )}
                            <span className="text-purple-400 font-mono font-bold text-sm">{data.rating}/10</span>
                            <button
                              onClick={() => startEdit(name, data)}
                              title="Edit skill"
                              className="text-gray-600 hover:text-indigo-300 transition-colors text-sm leading-none p-0.5"
                            >
                              {'✎'}
                            </button>
                          </span>
                        </div>
                        <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden mb-1.5">
                          <div
                            className="h-full bg-purple-500 rounded-full transition-all"
                            style={{ width: `${(data.rating / 10) * 100}%` }}
                          />
                        </div>
                        {(data.tier ?? 1) > 1 && data.evolution_theme && (
                          <div className="text-[10px] text-amber-400/80 mb-1">
                            Tier {data.tier} evolution {'•'} {data.evolution_theme} path
                          </div>
                        )}
                        {data.description && (
                          <div className="text-[11px] text-gray-400 leading-relaxed">{data.description}</div>
                        )}
                        {data.trigger_words && data.trigger_words.length > 0 && (
                          <div className="flex flex-wrap gap-1 mt-1.5">
                            {data.trigger_words.map((w, i) => (
                              <span key={i} className="text-[10px] px-1.5 py-0.5 bg-gray-700/60 text-gray-500 rounded">
                                {w}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    )
                  ))}
                  {editingSkill === NEW_SKILL ? (
                    <SkillEditForm
                      form={form}
                      setForm={setForm}
                      saving={saving}
                      saveError={saveError}
                      onSave={saveSkill}
                      onCancel={cancelEdit}
                    />
                  ) : (
                    <button
                      onClick={startAdd}
                      className="w-full py-1.5 px-3 text-xs text-gray-400 hover:text-indigo-300 bg-gray-800/40 hover:bg-gray-800/70 border border-dashed border-gray-700 rounded-lg transition-colors"
                    >
                      + Add Skill
                    </button>
                  )}
                </div>
              </section>
            </div>
          </div>
        </div>
      )}

      {showLevelUp && (
        <LevelUpModal
          rpg={rpg}
          config={config}
          generating={generating}
          onClose={() => setShowLevelUp(false)}
          onApplied={(newRpg) => { setRpgOverride(newRpg); setSkillsOverride(null); setShowLevelUp(false); }}
        />
      )}

      {evolvingSkill && rpg.skills?.[evolvingSkill] && (
        <SkillEvolutionModal
          skillName={evolvingSkill}
          rpg={rpg}
          generating={generating}
          onDeferred={(newRpg) => { if (newRpg) { setRpgOverride(newRpg); setSkillsOverride(null); } setEvolvingSkill(null); }}
          onEvolved={(newRpg) => { setRpgOverride(newRpg); setSkillsOverride(null); setEvolvingSkill(null); }}
        />
      )}
    </>
  );
}
