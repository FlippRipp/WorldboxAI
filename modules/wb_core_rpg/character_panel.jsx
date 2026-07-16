import React from 'react';

// Character-view panel for the Core RPG System. Rendered inside the unified
// storyteller Character View (see frontend CharacterView.jsx) for any module
// that declares `character_panel` in its manifest. Receives { state, config }.

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

const TYPE_STYLES = {
  active: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  passive: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  curse: 'bg-red-500/20 text-red-400 border-red-500/30',
};

const TYPE_LABELS = { active: 'Active', passive: 'Passive', curse: 'Curse' };

function tierFor(val, tiers) {
  for (const t of tiers) {
    if (val >= t.min && val <= t.max) return t.label;
  }
  return 'Unknown';
}

function xpForLevel(level, steepness = 2) {
  return Math.floor(50 * Math.pow(level, steepness));
}

function totalXpForLevel(level, steepness = 2) {
  let total = 0;
  for (let n = 1; n < level; n++) total += xpForLevel(n, steepness);
  return total;
}

function statBarColor(val, max) {
  const pct = val / max;
  if (pct >= 0.8) return 'bg-indigo-500';
  if (pct >= 0.6) return 'bg-blue-500';
  if (pct >= 0.4) return 'bg-cyan-500';
  return 'bg-slate-500';
}

function effectDurationLabel(effect, nowMinutes) {
  if (effect.duration_turns != null) {
    return `${effect.duration_turns} turn${effect.duration_turns !== 1 ? 's' : ''} left`;
  }
  if (effect.expires_at_minutes != null && nowMinutes != null) {
    const left = Math.max(0, effect.expires_at_minutes - nowMinutes);
    if (left >= 1440) return `~${Math.round(left / 1440)}d left`;
    if (left >= 60) return `~${Math.round(left / 60)}h left`;
    return `${left}m left`;
  }
  return 'ongoing';
}

export default function CharacterPanel({ state, config }) {
  const rpg = state?.module_data?.wb_core_rpg;
  if (!rpg) return <div className="text-sm text-gray-500 italic">No RPG data.</div>;

  const hp = rpg.hp ?? 0;
  const maxHp = rpg.max_hp ?? 100;
  const level = rpg.level ?? 1;
  const xp = rpg.xp ?? 0;
  const stats = rpg.stats ?? {};
  const skills = rpg.skills ?? {};
  const tiers = rpg.stat_tiers ?? FALLBACK_TIERS;
  const backstory = rpg.backstory ?? '';
  const unconscious = hp <= 0;
  const statusEffects = Array.isArray(rpg.status_effects) ? rpg.status_effects : [];
  const nowMinutes = state?.module_data?.wb_time_tracker?.clock?.total_minutes_elapsed ?? null;

  const hpPct = maxHp > 0 ? Math.max(0, Math.min(100, (hp / maxHp) * 100)) : 100;
  const hpColor = hpPct > 60 ? 'bg-green-500' : hpPct > 30 ? 'bg-yellow-500' : 'bg-red-500';

  const steepness = config?.xp_curve_steepness ?? 2;
  const xpNeeded = totalXpForLevel(level + 1, steepness);
  const xpCurrent = totalXpForLevel(level, steepness);
  const xpIntoLevel = xp - xpCurrent;
  const xpForThisLevel = xpNeeded - xpCurrent;
  const xpPct = xpForThisLevel > 0 ? Math.min(100, (xpIntoLevel / xpForThisLevel) * 100) : 100;

  const maxStatVal = Math.max(20, ...Object.values(stats));
  const skillEntries = Object.entries(skills).sort((a, b) => b[1].rating - a[1].rating);
  // Progression off: ratings are frozen and never rolled, so the per-skill
  // rating numbers/bars are hidden (matches the sidebar widget).
  const progressionOn = config?.skill_progression_enabled !== false;

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-2">
        <span className="text-xs px-2 py-0.5 bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 rounded-full font-mono">
          Level {level}
        </span>
        {(rpg.unspent_attribute_points ?? 0) + (rpg.unspent_skill_points ?? 0) > 0 && (
          <span className="text-xs px-2 py-0.5 bg-amber-500/20 text-amber-300 border border-amber-500/40 rounded-full">
            {(rpg.unspent_attribute_points ?? 0) + (rpg.unspent_skill_points ?? 0)} unspent point{(rpg.unspent_attribute_points ?? 0) + (rpg.unspent_skill_points ?? 0) === 1 ? '' : 's'}
          </span>
        )}
        {unconscious && <span className="text-red-400 text-xs animate-pulse">UNCONSCIOUS</span>}
      </div>

      {backstory && (
        <section>
          <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Backstory</h4>
          <p className="text-sm text-gray-300 leading-relaxed bg-gray-800/40 rounded-lg p-3 border border-gray-700/50">{backstory}</p>
        </section>
      )}

      <section>
        <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-3">Vitals</h4>
        <div className="space-y-3">
          <div>
            <div className="flex justify-between text-xs mb-1">
              <span className="text-gray-400">HP</span>
              <span className={`font-mono ${unconscious ? 'text-red-400' : 'text-gray-200'}`}>{hp}/{maxHp}</span>
            </div>
            <div className="w-full h-3 bg-gray-800 rounded-full overflow-hidden">
              <div className={`h-full ${hpColor} transition-all duration-500 rounded-full`} style={{ width: `${hpPct}%` }} />
            </div>
          </div>
          <div>
            <div className="flex justify-between text-xs mb-1">
              <span className="text-gray-400">XP</span>
              <span className="text-gray-200 font-mono">{xpIntoLevel}/{xpForThisLevel} ({xpPct.toFixed(0)}%)</span>
            </div>
            <div className="w-full h-2 bg-gray-800 rounded-full overflow-hidden">
              <div className="h-full bg-amber-500 transition-all duration-500 rounded-full" style={{ width: `${xpPct}%` }} />
            </div>
            <div className="text-[10px] text-gray-600 mt-1">Total XP: {xp}</div>
          </div>
        </div>
      </section>

      {statusEffects.length > 0 && (
        <section>
          <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-3">Status Effects ({statusEffects.length})</h4>
          <div className="space-y-2">
            {statusEffects.map((e) => (
              <div
                key={e.name}
                className={`rounded-lg border p-3 ${e.kind === 'good' ? 'bg-emerald-500/10 border-emerald-500/30' : 'bg-red-500/10 border-red-500/30'}`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className={`font-medium capitalize text-sm ${e.kind === 'good' ? 'text-emerald-300' : 'text-red-300'}`}>{e.name}</span>
                  <span className="text-gray-500 font-mono text-xs">
                    {e.severity != null ? `severity ${e.severity}/10 · ` : ''}{effectDurationLabel(e, nowMinutes)}
                  </span>
                </div>
                {e.description && <div className="text-[11px] text-gray-400 leading-relaxed">{e.description}</div>}
              </div>
            ))}
          </div>
        </section>
      )}

      <section>
        <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-3">Attributes</h4>
        <div className="grid grid-cols-2 gap-3">
          {Object.entries(stats).map(([stat, value]) => (
            <div key={stat} className="bg-gray-800/50 rounded-lg border border-gray-700/50 p-3">
              <div className="flex items-center justify-between mb-1">
                <span className="text-gray-200 font-medium capitalize text-sm">{stat}</span>
                <span className="text-indigo-400 font-mono font-bold text-sm">{value}</span>
              </div>
              <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden mb-1.5">
                <div className={`h-full rounded-full transition-all ${statBarColor(value, maxStatVal)}`} style={{ width: `${Math.min(100, (value / maxStatVal) * 100)}%` }} />
              </div>
              <div className="text-[11px] text-amber-400/80 mb-1">{tierFor(value, tiers)}</div>
              <div className="text-[10px] text-gray-600 leading-tight">{STAT_DESCRIPTIONS[stat] || ''}</div>
            </div>
          ))}
        </div>
      </section>

      {skillEntries.length > 0 && (
        <section>
          <h4 className="text-xs uppercase tracking-wider text-gray-500 mb-3">Skills ({skillEntries.length})</h4>
          <div className="space-y-2">
            {skillEntries.map(([name, data]) => (
              <div key={name} className="bg-gray-800/50 rounded-lg border border-gray-700/50 p-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-gray-200 font-medium flex items-center gap-1.5 text-sm">
                    <span className="capitalize">{name}</span>
                    {(data.tier ?? 1) > 1 && (
                      <span className="text-[9px] px-1 py-px rounded border bg-amber-500/20 text-amber-400 border-amber-500/30 font-semibold">
                        T{data.tier}
                      </span>
                    )}
                    <span className={`text-[10px] px-1.5 py-0.5 rounded border ${TYPE_STYLES[data.type] || TYPE_STYLES.active}`}>{TYPE_LABELS[data.type] || TYPE_LABELS.active}</span>
                  </span>
                  {progressionOn && <span className="text-purple-400 font-mono font-bold text-sm">{data.rating}/10</span>}
                </div>
                {progressionOn && (
                  <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden mb-1.5">
                    <div className="h-full bg-purple-500 rounded-full transition-all" style={{ width: `${(data.rating / 10) * 100}%` }} />
                  </div>
                )}
                {(data.tier ?? 1) > 1 && data.evolution_theme && (
                  <div className="text-[10px] text-amber-400/80 mb-1">Tier {data.tier} evolution {'•'} {data.evolution_theme} path</div>
                )}
                {data.description && <div className="text-[11px] text-gray-400 leading-relaxed">{data.description}</div>}
                {data.trigger_words && data.trigger_words.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1.5">
                    {data.trigger_words.map((w, i) => (
                      <span key={i} className="text-[10px] px-1.5 py-0.5 bg-gray-700/60 text-gray-500 rounded">{w}</span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
