import React, { useState } from 'react';

const MOMENTUM_STYLES = {
  observing: 'text-gray-500',
  building: 'text-indigo-400',
  steady: 'text-emerald-400',
  stalled: 'text-amber-400',
  resolving: 'text-purple-400',
};

const OUTCOME_ICONS = { resolved: '✓', abandoned: '✕', expired: '⌛', rerolled: '↻' };

export default function PlotDirectorWidget({ state, config, onCommand }) {
  const [open, setOpen] = useState(false);
  const [showLikes, setShowLikes] = useState(false);

  const data = state?.module_data?.wb_plot_director;
  if (!data) return null;
  if (config?.plot_enabled === false) return null;

  const legacy = data.schema !== 2;
  const thread = data.thread ?? {};
  const profile = data.profile ?? {};
  const momentum = legacy ? 'observing' : (data.momentum ?? 'observing');
  const hasThread = !legacy && data.status === 'active' && thread.status === 'active';
  const streak = data.ignored_streak ?? 0;
  const abandonAfter = config?.abandon_after ?? 4;
  const history = (data.thread_history ?? []).slice(-3).reverse();
  const topStyles = Object.entries(profile.playstyle ?? {})
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3);
  const likes = profile.likes ?? [];

  const subtitle = hasThread
    ? thread.title
    : data.status === 'failed'
      ? 'Inactive'
      : legacy || data.status === 'observing'
        ? 'Observing your story…'
        : 'Weaving a new thread…';

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="w-full bg-gray-900/70 rounded-lg border border-gray-700 p-3 text-left hover:border-gray-500 transition-colors"
      >
        <div className="flex items-center justify-between text-sm">
          <span className="text-gray-300 font-semibold">🎭 Plot</span>
          <span className={`text-xs capitalize ${MOMENTUM_STYLES[momentum] ?? 'text-gray-500'}`}>
            {momentum}
          </span>
        </div>
        <div className="text-xs text-gray-500 truncate mt-1">{subtitle}</div>
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setOpen(false)} />
          <div className="relative w-full max-w-md mx-4 max-h-[80vh] overflow-y-auto bg-gray-800 border border-gray-700 rounded-xl shadow-2xl p-4 space-y-4 text-sm">
            <div className="flex items-center justify-between">
              <span className="text-gray-100 font-semibold text-base">Plot Thread</span>
              <div className="flex items-center gap-3">
                <span className={`text-xs capitalize ${MOMENTUM_STYLES[momentum] ?? 'text-gray-500'}`}>
                  {momentum}
                </span>
                <button
                  onClick={() => setOpen(false)}
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
              <div className="text-xs text-gray-500 italic">{subtitle}</div>
            ) : (
              <div className="bg-gray-900/70 rounded-lg border border-gray-700 p-3 space-y-1.5">
                <div className="text-gray-100 font-semibold leading-snug">{thread.title}</div>
                {thread.hook && (
                  <div className="text-xs text-gray-400 leading-snug">{thread.hook}</div>
                )}
                {thread.challenge && (
                  <div className="text-xs leading-snug">
                    <span className="text-amber-400 uppercase tracking-wider text-[10px] mr-1">Challenge</span>
                    <span className="text-gray-300">{thread.challenge}</span>
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
                    {streak === 0 ? 'engaged' : `drifting ${streak}/${abandonAfter}`}
                  </span>
                </div>
              </div>
            )}

            {(topStyles.length > 0 || profile.tone || likes.length > 0) && (
              <div className="space-y-1.5 border-t border-gray-700 pt-3">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Your story profile</div>
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
                {profile.tone && (
                  <div className="text-[10px] text-gray-500">tone: {profile.tone}</div>
                )}
                {likes.length > 0 && (
                  <>
                    <button
                      onClick={() => setShowLikes(!showLikes)}
                      className="w-full flex items-center justify-between text-[10px] text-gray-500 hover:text-gray-300 transition-colors"
                    >
                      <span className="uppercase tracking-wider">Enjoying ({likes.length})</span>
                      <span className="text-gray-600">{showLikes ? '▼' : '▶'}</span>
                    </button>
                    {showLikes && (
                      <div className="space-y-0.5">
                        {likes.map((like, i) => (
                          <div key={`${like}-${i}`} className="text-[10px] text-gray-400">{like}</div>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            )}

            {history.length > 0 && (
              <div className="space-y-0.5 border-t border-gray-700 pt-3">
                <div className="text-[10px] text-gray-500 uppercase tracking-wider">Recent threads</div>
                {history.map((entry, i) => (
                  <div key={`${entry.title}-${i}`} className="text-[10px] text-gray-600 truncate">
                    {OUTCOME_ICONS[entry.outcome] ?? '·'} {entry.outcome} — {entry.title}
                  </div>
                ))}
              </div>
            )}

            {!legacy && data.status !== 'failed' && (
              <div className="border-t border-gray-700 pt-3">
                <button
                  onClick={() => onCommand?.('/plot regen')}
                  disabled={!onCommand}
                  className="w-full py-2 rounded-lg bg-indigo-600/80 hover:bg-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed text-gray-100 text-xs font-semibold transition-colors"
                >
                  ↻ Weave a new thread
                </button>
                <div className="text-[10px] text-gray-600 mt-1.5 text-center">
                  Closes the current thread and generates a fresh one from your profile.
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
