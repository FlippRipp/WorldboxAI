import React from 'react';

// Character-view panel for the Player Character Tracker. Shows the running log
// of lasting changes to the character's appearance, identity, and personality
// that the tracker has detected over the course of the story.
// Receives { state, config } from the unified Character View.

export default function CharacterPanel({ state }) {
  const data = state?.module_data?.wb_character_tracker || {};
  const log = Array.isArray(data.evolution_log) ? data.evolution_log : [];

  if (log.length === 0) {
    return (
      <div className="text-sm text-gray-500 italic">
        No changes recorded yet. As the story unfolds, lasting changes to your character will appear here.
      </div>
    );
  }

  const ordered = [...log].reverse();

  return (
    <div className="space-y-2">
      {ordered.map((entry, i) => (
        <div key={i} className="flex items-start gap-3 bg-gray-800/50 rounded-lg border border-gray-700/50 p-3">
          <span className="text-[10px] font-mono text-indigo-300 bg-indigo-500/15 border border-indigo-500/30 rounded px-1.5 py-0.5 whitespace-nowrap mt-0.5">
            Turn {entry.turn ?? '?'}
          </span>
          <div className="min-w-0">
            <p className="text-sm text-gray-300 leading-snug">{entry.note || 'Character updated.'}</p>
            {Array.isArray(entry.fields) && entry.fields.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">
                {entry.fields.map((f, j) => (
                  <span key={j} className="text-[10px] px-1.5 py-0.5 bg-gray-700/60 text-gray-400 rounded capitalize">
                    {String(f).replace(/_/g, ' ')}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
