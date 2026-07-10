import React, { useState } from 'react';

const MOMENTUM_STYLES = {
  observing: 'text-gray-500',
  building: 'text-indigo-400',
  steady: 'text-emerald-400',
  stalled: 'text-amber-400',
  resolving: 'text-purple-400',
};

const OUTCOME_ICONS = { resolved: '✓', abandoned: '✕', expired: '⌛' };

export default function PlotDirectorWidget({ state, config }) {
  const [showLikes, setShowLikes] = useState(false);

  const data = state?.module_data?.wb_plot_director;
  if (!data) return null;
  if (config?.plot_enabled === false) return null;

  // Legacy v1 save: data migrates on the next turn; show the observing
  // placeholder instead of vanishing until then.
  if (data.schema !== 2) {
    return (
      <div className="bg-gray-900/70 rounded-lg border border-gray-700 p-3 space-y-3 text-sm">
        <div className="flex items-center justify-between">
          <span className="text-gray-300 font-semibold">Plot Thread</span>
          <span className="text-xs text-gray-500">observing</span>
        </div>
        <div className="text-xs text-gray-500 italic">Observing your story…</div>
      </div>
    );
  }

  if (data.status === 'failed') {
    return (
      <div className="bg-gray-900/70 rounded-lg border border-gray-700 p-3 text-xs text-gray-600">
        Plot direction inactive.
      </div>
    );
  }

  const thread = data.thread ?? {};
  const profile = data.profile ?? {};
  const momentum = data.momentum ?? 'observing';
  const streak = data.ignored_streak ?? 0;
  const abandonAfter = config?.abandon_after ?? 4;
  const history = (data.thread_history ?? []).slice(-2).reverse();
  const topStyles = Object.entries(profile.playstyle ?? {})
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3);
  const likes = profile.likes ?? [];

  return (
    <div className="bg-gray-900/70 rounded-lg border border-gray-700 p-3 space-y-3 text-sm">
      <div className="flex items-center justify-between">
        <span className="text-gray-300 font-semibold">Plot Thread</span>
        <span className={`text-xs capitalize ${MOMENTUM_STYLES[momentum] ?? 'text-gray-500'}`}>
          {momentum}
        </span>
      </div>

      {data.status === 'observing' ? (
        <div className="text-xs text-gray-500 italic">Observing your story…</div>
      ) : thread.status !== 'active' ? (
        <div className="text-xs text-gray-500 italic">Weaving a new thread…</div>
      ) : (
        <div className="space-y-1.5">
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
        <div className="space-y-1.5 border-t border-gray-800 pt-2">
          {topStyles.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {topStyles.map(([key, value]) => (
                <span
                  key={key}
                  className="bg-gray-800 border border-gray-700 rounded-full text-[10px] text-gray-300 px-2 py-0.5"
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
        <div className="space-y-0.5 border-t border-gray-800 pt-2">
          {history.map((entry, i) => (
            <div key={`${entry.title}-${i}`} className="text-[10px] text-gray-600 truncate">
              {OUTCOME_ICONS[entry.outcome] ?? '·'} {entry.outcome} — {entry.title}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
