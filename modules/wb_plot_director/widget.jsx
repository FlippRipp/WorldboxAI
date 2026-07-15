import React, { useState, useEffect } from 'react';

const MOMENTUM_STYLES = {
  observing: 'text-gray-500',
  building: 'text-indigo-400',
  steady: 'text-emerald-400',
  stalled: 'text-amber-400',
  resolving: 'text-purple-400',
};

const OUTCOME_ICONS = { resolved: '✓', abandoned: '✕', expired: '⌛', rerolled: '↻', superseded: '↷' };

const PREF_WEIGHTS = ['low', 'medium', 'high'];
const WEIGHT_CHIP_STYLES = {
  high: 'border-indigo-500/70 text-gray-100',
  medium: 'border-gray-700 text-gray-300',
  low: 'border-gray-700 text-gray-500',
};
const WEIGHT_BADGE = { low: 'L', medium: 'M', high: 'H' };

// Weighted lists (likes/dislikes/avoids) store {text, weight} objects (plus
// an evidence clause on observed lists); themes are plain strings. Tolerate
// both shapes -- old saves normalize server-side on the next write.
function normalizeEntry(entry) {
  if (entry && typeof entry === 'object') {
    return {
      text: entry.text ?? '',
      weight: PREF_WEIGHTS.includes(entry.weight) ? entry.weight : 'medium',
      evidence: entry.evidence ?? '',
    };
  }
  return { text: String(entry ?? ''), weight: 'medium', evidence: '' };
}

// Whether the global cheats.enabled engine setting is on. The reset button is
// hidden without it; the command itself stays reachable via
// '/plot reset confirm' for players who type it deliberately.
function useCheatMode() {
  const [cheatMode, setCheatMode] = useState(false);
  useEffect(() => {
    let cancelled = false;
    fetch('/api/settings?scope=global')
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled || !data?.settings) return;
        for (const group of Object.values(data.settings)) {
          const hit = (group || []).find((d) => d.key === 'cheats.enabled');
          if (hit) { setCheatMode(!!hit.value); return; }
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);
  return cheatMode;
}

// Blurred until clicked, so spoiler material (the thread's opposition, the
// story's larger arc) stays a surprise unless the player opts in. Mount with
// a key tied to the content's identity (thread id, direction update turn) so
// fresh material re-hides itself.
function Spoiler({ label = 'spoiler', children }) {
  const [revealed, setRevealed] = useState(false);
  if (revealed) return children;
  return (
    <button
      onClick={() => setRevealed(true)}
      className="w-full text-left group"
      aria-label={`Reveal ${label} (spoiler)`}
      title="Spoiler — click to reveal"
    >
      <span className="blur-[5px] select-none group-hover:blur-[4px] transition-all">{children}</span>
      <span className="block text-[9px] text-gray-600 group-hover:text-gray-400">spoiler — click to reveal</span>
    </button>
  );
}

// A read-only chip row for AI-maintained lists (engagement patterns,
// recurring story elements).
function ChipRow({ label, items, className = 'text-gray-400' }) {
  const texts = (items ?? []).map((t) => String(t ?? '').trim()).filter(Boolean);
  if (texts.length === 0) return null;
  return (
    <div className="space-y-1">
      <div className="text-[10px] text-gray-500 uppercase tracking-wider">{label}</div>
      <div className="flex flex-wrap gap-1">
        {texts.map((t, i) => (
          <span
            key={`${t}-${i}`}
            className={`bg-gray-900 border border-gray-700 rounded-full text-[10px] px-2 py-0.5 ${className}`}
          >
            {t}
          </span>
        ))}
      </div>
    </div>
  );
}

// One editable profile list: chips with remove buttons in edit mode, plus an
// add input. Weighted lists also get a low/medium/high picker on add and a
// per-chip badge that cycles the weight. Persistence is a command round-trip;
// the refreshed list arrives via the next state_update. removeOnly lists
// (avoids -- the AI writes them, the player can only veto) get no add input
// and no weight cycling.
function ProfileList({ label, field, entries, weighted, editing, onCommand, hint, removeOnly }) {
  const [draft, setDraft] = useState('');
  const [draftWeight, setDraftWeight] = useState('medium');

  if (!editing && entries.length === 0) return null;

  const items = entries.map(normalizeEntry);
  if (weighted) {
    items.sort((a, b) => PREF_WEIGHTS.indexOf(b.weight) - PREF_WEIGHTS.indexOf(a.weight));
  }

  const add = () => {
    const text = draft.trim();
    if (!text) return;
    onCommand?.(`/plot profile ${field} add ${weighted ? `${draftWeight} ` : ''}${text}`);
    setDraft('');
  };

  const cycleWeight = (item) => {
    const next = PREF_WEIGHTS[(PREF_WEIGHTS.indexOf(item.weight) + 1) % PREF_WEIGHTS.length];
    // Re-adding an existing entry with a different weight updates it in place.
    onCommand?.(`/plot profile ${field} add ${next} ${item.text}`);
  };

  return (
    <div className="space-y-1">
      <div className="text-[10px] text-gray-500 uppercase tracking-wider">
        {label}
        {hint && editing && <span className="normal-case tracking-normal text-gray-600 ml-1.5">— {hint}</span>}
      </div>
      <div className="flex flex-wrap gap-1">
        {items.map((item, i) => (
          <span
            key={`${item.text}-${i}`}
            title={item.evidence || undefined}
            className={`flex items-center gap-1 bg-gray-900 border rounded-full text-[10px] px-2 py-0.5 ${
              weighted ? (WEIGHT_CHIP_STYLES[item.weight] ?? WEIGHT_CHIP_STYLES.medium) : 'border-gray-700 text-gray-300'
            }`}
          >
            {weighted && editing && !removeOnly && (
              <button
                onClick={() => cycleWeight(item)}
                className="font-mono text-[9px] text-indigo-400 hover:text-indigo-300"
                title={`Weight: ${item.weight} (click to change)`}
                aria-label={`Change weight of ${item.text} (currently ${item.weight})`}
              >
                {WEIGHT_BADGE[item.weight]}
              </button>
            )}
            {weighted && !editing && item.weight === 'high' && <span className="text-indigo-400">★</span>}
            {item.text}
            {editing && (
              <button
                onClick={() => onCommand?.(`/plot profile ${field} remove ${item.text}`)}
                className="text-gray-500 hover:text-red-400 transition-colors"
                aria-label={`Remove ${item.text} from ${label}`}
              >
                ✕
              </button>
            )}
          </span>
        ))}
        {items.length === 0 && (
          <span className="text-[10px] text-gray-600 italic">none yet</span>
        )}
      </div>
      {editing && !removeOnly && (
        <div className="flex gap-1">
          {weighted && (
            <select
              value={draftWeight}
              onChange={(e) => setDraftWeight(e.target.value)}
              className="bg-gray-900 border border-gray-700 rounded px-1 py-1 text-[11px] text-gray-300 focus:outline-none focus:border-indigo-500"
              aria-label={`Weight for new ${label.toLowerCase()} entry`}
            >
              {PREF_WEIGHTS.map((w) => <option key={w} value={w}>{w}</option>)}
            </select>
          )}
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') add(); }}
            placeholder={`Add to ${label.toLowerCase()}…`}
            className="flex-1 min-w-0 bg-gray-900 border border-gray-700 rounded px-2 py-1 text-[11px] text-gray-200 placeholder-gray-600 focus:outline-none focus:border-indigo-500"
          />
          <button
            onClick={add}
            disabled={!draft.trim()}
            className="px-2 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-[11px] text-gray-200"
          >
            +
          </button>
        </div>
      )}
    </div>
  );
}

export default function PlotDirectorWidget({ state, config, onCommand }) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [toneDraft, setToneDraft] = useState(null);
  const [resetArmed, setResetArmed] = useState(false);
  // Optimistic wipe: set on reset-confirm so the panel empties immediately as
  // feedback while the rebuild command (analysis + direction + thread) runs
  // server-side; the next state_update replaces the module data and clears it.
  const [resetting, setResetting] = useState(false);
  // Same idea for the regen button: /plot regen takes a few LLM calls, so the
  // button shows a busy state until the command's state_update lands (the
  // server sends one after every command, success or failure).
  const [regenning, setRegenning] = useState(false);
  const cheatMode = useCheatMode();

  const liveData = state?.module_data?.wb_plot_director;
  useEffect(() => { setResetting(false); setRegenning(false); }, [liveData]);
  if (!liveData) return null;
  if (config?.plot_enabled === false) return null;

  // While resetting, render from a blank slate instead of the doomed data.
  const data = resetting
    ? { schema: liveData.schema, status: 'observing', thread: { status: 'none' },
        profile: {}, direction: {}, thread_history: [], momentum: 'observing',
        ignored_streak: 0 }
    : liveData;

  const legacy = !(data.schema >= 2);
  const thread = data.thread ?? {};
  const profile = data.profile ?? {};
  const direction = data.direction ?? {};
  const suspended = !legacy && data.suspended === true;
  const momentum = legacy ? 'observing' : (data.momentum ?? 'observing');
  const momentumLabel = suspended ? 'suspended' : momentum;
  const momentumStyle = suspended ? 'text-gray-500' : (MOMENTUM_STYLES[momentum] ?? 'text-gray-500');
  const hasThread = !legacy && data.status === 'active' && thread.status === 'active';
  const breathing = !legacy && !hasThread && data.status === 'active'
    && (state?.turn ?? 0) < (data.next_thread_turn ?? 0);
  const streak = data.ignored_streak ?? 0;
  const abandonAfter = config?.abandon_after ?? 4;
  const history = (data.thread_history ?? []).slice(-3).reverse();
  const topStyles = Object.entries(profile.playstyle ?? {})
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3);
  const tone = profile.tone ?? '';
  const narrative = profile.narrative ?? {};
  const narrativeLine = ['pacing', 'agency', 'register']
    .map((k) => (narrative[k] ? `${k}: ${narrative[k]}` : null))
    .filter(Boolean)
    .join(' · ');
  const attachments = (profile.attachments ?? []).filter((a) => a && a.name);
  const hasProfile = topStyles.length > 0 || tone
    || (profile.themes ?? []).length > 0 || (profile.likes ?? []).length > 0
    || (profile.dislikes ?? []).length > 0 || (profile.avoids ?? []).length > 0
    || attachments.length > 0 || narrativeLine || profile.notes;
  const hasDirection = !legacy && (direction.premise ?? '').trim() !== '';

  const subtitle = resetting
    ? 'Plot data cleared — rebuilding from your story…'
    : suspended
      ? (hasThread ? `⏸ ${thread.title}` : 'Suspended')
      : hasThread
        ? thread.title
        : data.status === 'failed'
          ? 'Inactive'
          : legacy || data.status === 'observing'
            ? 'Observing your story…'
            : breathing
              ? 'Letting the story breathe…'
              : 'Weaving a new thread…';

  const saveTone = () => {
    if (toneDraft === null || toneDraft.trim() === tone) { setToneDraft(null); return; }
    onCommand?.(`/plot profile tone ${toneDraft.trim()}`);
    setToneDraft(null);
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="w-full bg-gray-900/70 rounded-lg border border-gray-700 p-3 text-left hover:border-gray-500 transition-colors"
      >
        <div className="flex items-center justify-between text-sm">
          <span className="text-gray-300 font-semibold">🎭 Plot</span>
          <span className={`text-xs capitalize ${momentumStyle}`}>
            {momentumLabel}
          </span>
        </div>
        <div className="text-xs text-gray-500 truncate mt-1">{subtitle}</div>
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => { setOpen(false); setEditing(false); }} />
          <div className="relative w-full max-w-md mx-4 max-h-[80vh] overflow-y-auto bg-gray-800 border border-gray-700 rounded-xl shadow-2xl p-4 space-y-4 text-sm">
            <div className="flex items-center justify-between">
              <span className="text-gray-100 font-semibold text-base">Plot Thread</span>
              <div className="flex items-center gap-3">
                <span className={`text-xs capitalize ${momentumStyle}`}>
                  {momentumLabel}
                </span>
                <button
                  onClick={() => { setOpen(false); setEditing(false); }}
                  className="p-1 text-gray-400 hover:text-white hover:bg-gray-700 rounded"
                  aria-label="Close plot view"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>

            {data.status === 'failed' ? (
              <div className="text-xs text-gray-600">Plot direction inactive.</div>
            ) : !hasThread ? (
              <div className={`text-xs text-gray-500 italic ${resetting ? 'animate-pulse' : ''}`}>{subtitle}</div>
            ) : (
              <div className="bg-gray-900/70 rounded-lg border border-gray-700 p-3 space-y-1.5">
                <div className="text-gray-100 font-semibold leading-snug">{thread.title}</div>
                {thread.hook && (
                  <div className="text-xs text-gray-400 leading-snug">{thread.hook}</div>
                )}
                {thread.challenge && (
                  <div className="text-xs leading-snug">
                    <span className="text-amber-400 uppercase tracking-wider text-[10px] mr-1">Challenge</span>
                    <Spoiler key={thread.id} label="the challenge">
                      <span className="text-gray-300">{thread.challenge}</span>
                    </Spoiler>
                  </div>
                )}
                {thread.stakes && (
                  <div className="text-xs leading-snug">
                    <span className="text-gray-500 uppercase tracking-wider text-[10px] mr-1">Stakes</span>
                    <span className="text-gray-400">{thread.stakes}</span>
                  </div>
                )}
                {thread.appeal && (
                  <div className="text-[10px] text-gray-600">for you: {thread.appeal}</div>
                )}
                <div className="flex items-center gap-1.5 pt-0.5">
                  {Array.from({ length: abandonAfter }, (_, i) => (
                    <span
                      key={i}
                      className={`w-1.5 h-1.5 rounded-full ${i < streak ? 'bg-amber-400' : 'bg-gray-700'}`}
                    />
                  ))}
                  <span className="text-[10px] text-gray-500 ml-1">
                    {suspended ? 'frozen' : streak === 0 ? 'engaged' : `drifting ${streak}/${abandonAfter}`}
                  </span>
                </div>
              </div>
            )}

            {!legacy && !hasDirection && data.status !== 'failed' && (
              <div className="space-y-1 border-t border-gray-700 pt-3">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Story direction</div>
                <div className="text-[10px] text-gray-600 italic">Still taking shape — it forms from the story's first turns.</div>
              </div>
            )}

            {hasDirection && (
              <div className="space-y-1 border-t border-gray-700 pt-3">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Story direction</div>
                {/* The arc is spoiler territory too: it re-hides whenever it evolves. */}
                <Spoiler key={direction.updated_turn ?? 0} label="the story direction">
                  <div className="text-xs text-gray-300 leading-snug">{direction.premise}</div>
                  {direction.heading && (
                    <div className="text-[10px] text-gray-500 leading-snug">{direction.heading}</div>
                  )}
                  {(direction.open_questions ?? []).length > 0 && (
                    <div className="flex flex-wrap gap-1 pt-0.5">
                      {direction.open_questions.map((q, i) => (
                        <span
                          key={`${q}-${i}`}
                          className="bg-gray-900 border border-gray-700 rounded-full text-[10px] text-gray-400 px-2 py-0.5"
                        >
                          {q}
                        </span>
                      ))}
                    </div>
                  )}
                  {(direction.recurring_elements ?? []).length > 0 && (
                    <div className="text-[10px] text-gray-600">
                      Recurring: {direction.recurring_elements.join(', ')}
                    </div>
                  )}
                </Spoiler>
              </div>
            )}

            {!legacy && (hasProfile || onCommand) && (
              <div className="space-y-2.5 border-t border-gray-700 pt-3">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] text-gray-500 uppercase tracking-wider">Your story profile</span>
                  {onCommand && (
                    <button
                      onClick={() => { setEditing(!editing); setToneDraft(null); }}
                      className={`text-[10px] px-2 py-0.5 rounded transition-colors ${
                        editing
                          ? 'bg-indigo-600/80 text-gray-100 hover:bg-indigo-500'
                          : 'text-gray-500 hover:text-gray-300 hover:bg-gray-700'
                      }`}
                    >
                      {editing ? 'Done' : 'Edit'}
                    </button>
                  )}
                </div>

                {topStyles.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {topStyles.map(([key, value]) => (
                      <span
                        key={key}
                        className="bg-gray-900 border border-gray-700 rounded-full text-[10px] text-gray-300 px-2 py-0.5"
                      >
                        {key} {value}
                      </span>
                    ))}
                  </div>
                )}

                {editing ? (
                  <div className="space-y-1">
                    <div className="text-[10px] text-gray-500 uppercase tracking-wider">Tone</div>
                    <input
                      value={toneDraft ?? tone}
                      onChange={(e) => setToneDraft(e.target.value)}
                      onBlur={saveTone}
                      onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur(); }}
                      placeholder="e.g. gritty, whimsical…"
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1 text-[11px] text-gray-200 placeholder-gray-600 focus:outline-none focus:border-indigo-500"
                    />
                  </div>
                ) : (
                  tone && <div className="text-[10px] text-gray-500">tone: {tone}</div>
                )}

                <ProfileList label="Themes" field="themes" entries={profile.themes ?? []} editing={editing} onCommand={onCommand} />
                <ProfileList label="Likes" field="likes" entries={profile.likes ?? []} weighted editing={editing} onCommand={onCommand} />
                <ProfileList label="Dislikes" field="dislikes" entries={profile.dislikes ?? []} weighted editing={editing} onCommand={onCommand} hint="yours alone; the AI never adds these" />
                <ProfileList label="Avoids" field="avoids" entries={profile.avoids ?? []} weighted editing={editing} onCommand={onCommand} removeOnly hint="observed by the AI; remove any that ring false" />

                {attachments.length > 0 && (
                  <div className="space-y-1">
                    <div className="text-[10px] text-gray-500 uppercase tracking-wider">Drawn to</div>
                    <div className="flex flex-wrap gap-1">
                      {attachments.map((a, i) => (
                        <span
                          key={`${a.name}-${i}`}
                          title={a.note || undefined}
                          className="bg-gray-900 border border-gray-700 rounded-full text-[10px] text-gray-300 px-2 py-0.5"
                        >
                          {a.name}
                          {a.kind && <span className="text-gray-600 ml-1">{a.kind}</span>}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                <ChipRow label="Hooks you bite on" items={profile.engagement?.bites_on} />
                <ChipRow label="Hooks you pass by" items={profile.engagement?.ignores} className="text-gray-500" />

                {narrativeLine && (
                  <div className="text-[10px] text-gray-500">{narrativeLine}</div>
                )}
                {profile.notes && (
                  <div className="text-[10px] text-gray-600 italic leading-snug">{profile.notes}</div>
                )}
              </div>
            )}

            {history.length > 0 && (
              <div className="space-y-0.5 border-t border-gray-700 pt-3">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Recent threads</div>
                {history.map((entry, i) => (
                  <div key={`${entry.title}-${i}`}>
                    <div className="text-[10px] text-gray-600 truncate">
                      {OUTCOME_ICONS[entry.outcome] ?? '·'} {entry.outcome} — {entry.title}
                    </div>
                    {entry.consequence && (
                      <div className="text-[10px] text-gray-700 truncate pl-4">{entry.consequence}</div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {!legacy && !resetting && data.status !== 'failed' && (
              <div className="border-t border-gray-700 pt-3 space-y-2">
                {!suspended && (
                  <div>
                    <button
                      onClick={() => { setRegenning(true); onCommand?.('/plot regen'); }}
                      disabled={!onCommand || regenning}
                      aria-busy={regenning}
                      className={`w-full py-2 rounded-lg bg-indigo-600/80 hover:bg-indigo-500 disabled:cursor-not-allowed text-gray-100 text-xs font-semibold transition-colors ${
                        regenning ? 'animate-pulse' : 'disabled:opacity-40'
                      }`}
                    >
                      {regenning ? '⏳ Weaving a new thread…' : '↻ Weave a new thread'}
                    </button>
                    <div className="text-[10px] text-gray-600 mt-1.5 text-center">
                      {regenning
                        ? 'Generating and quality-checking a fresh thread — a few seconds.'
                        : 'Closes the current thread and generates a fresh one from your profile.'}
                    </div>
                  </div>
                )}
                <div>
                  <button
                    onClick={() => onCommand?.(suspended ? '/plot resume' : '/plot suspend')}
                    disabled={!onCommand}
                    className={`w-full py-2 rounded-lg disabled:opacity-40 disabled:cursor-not-allowed text-xs font-semibold transition-colors ${
                      suspended
                        ? 'bg-emerald-600/80 hover:bg-emerald-500 text-gray-100'
                        : 'bg-gray-700 hover:bg-gray-600 text-gray-300'
                    }`}
                  >
                    {suspended ? '▶ Resume plot direction' : '⏸ Suspend plot direction'}
                  </button>
                  <div className="text-[10px] text-gray-600 mt-1.5 text-center">
                    {suspended
                      ? 'Picks the thread back up where it left off.'
                      : 'Freezes the thread and all plot background calls until you resume.'}
                  </div>
                </div>
              </div>
            )}

            {!legacy && cheatMode && (
              <div className="border-t border-gray-700 pt-3">
                <button
                  onClick={() => {
                    if (!resetArmed) { setResetArmed(true); return; }
                    setResetArmed(false);
                    setResetting(true);
                    setEditing(false);
                    onCommand?.('/plot reset confirm');
                  }}
                  onBlur={() => setResetArmed(false)}
                  disabled={!onCommand || resetting}
                  className={`w-full py-2 rounded-lg disabled:opacity-40 disabled:cursor-not-allowed text-xs font-semibold transition-colors ${
                    resetArmed
                      ? 'bg-red-600/90 hover:bg-red-500 text-gray-100'
                      : 'bg-gray-800 border border-red-900/60 hover:border-red-700 text-red-400'
                  }`}
                >
                  {resetting
                    ? '⏳ Rebuilding from your story…'
                    : resetArmed
                      ? '⚠ Really wipe everything? Click again'
                      : '☠ Reset plot data (cheat)'}
                </button>
                <div className="text-[10px] text-gray-600 mt-1.5 text-center">
                  Clears the observed profile, story direction, and thread history, then rebuilds
                  them from the story so far. Likes and dislikes you added yourself are kept.
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
