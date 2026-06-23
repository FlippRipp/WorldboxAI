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

export default function CoreRpgWidget({ state, config }) {
  const [showStats, setShowStats] = useState(true);
  const [showSkills, setShowSkills] = useState(true);
  const [showFullSheet, setShowFullSheet] = useState(false);

  useEffect(() => {
    if (!showFullSheet) return;
    function onKey(e) { if (e.key === 'Escape') setShowFullSheet(false); }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [showFullSheet]);

  const rpg = state?.module_data?.wb_core_rpg;
  if (!rpg) return null;

  const hp = rpg.hp ?? 0;
  const maxHp = rpg.max_hp ?? 100;
  const level = rpg.level ?? 1;
  const xp = rpg.xp ?? 0;
  const stats = rpg.stats ?? {};
  const skills = rpg.skills ?? {};
  const tiers = rpg.stat_tiers ?? FALLBACK_TIERS;
  const backstory = rpg.backstory ?? '';
  const unconscious = hp <= 0;

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
                        <span className={`text-[9px] px-1 py-px rounded border ${TYPE_STYLES[data.type] || TYPE_STYLES.active}`}>{TYPE_LABELS[data.type] || TYPE_LABELS.active}</span>
                      </span>
                      <span className="text-purple-400 font-mono ml-2">{data.rating}/10</span>
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
              {skillEntries.length > 0 && (
                <section>
                  <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-3">
                    Skills ({skillEntries.length})
                  </h3>
                  <div className="space-y-2">
                    {skillEntries.map(([name, data]) => (
                      <div key={name} className="bg-gray-800/50 rounded-lg border border-gray-700/50 p-3">
                        <div className="flex items-center justify-between mb-1">
                          <span className="text-gray-200 font-medium flex items-center gap-1.5 text-sm">
                            <span className="capitalize">{name}</span>
                            <span className={`text-[10px] px-1.5 py-0.5 rounded border ${TYPE_STYLES[data.type] || TYPE_STYLES.active}`}>{TYPE_LABELS[data.type] || TYPE_LABELS.active}</span>
                          </span>
                          <span className="text-purple-400 font-mono font-bold text-sm">{data.rating}/10</span>
                        </div>
                        <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden mb-1.5">
                          <div
                            className="h-full bg-purple-500 rounded-full transition-all"
                            style={{ width: `${(data.rating / 10) * 100}%` }}
                          />
                        </div>
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
                    ))}
                  </div>
                </section>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
