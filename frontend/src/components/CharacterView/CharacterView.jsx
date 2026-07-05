import { useEffect } from 'react';
import WidgetErrorBoundary from '../shared/WidgetErrorBoundary';
import DynamicWidget from '../shared/DynamicWidget';

// Unified character view for the storyteller. Shows the story character's core
// identity (from state.characters.default_player) plus any additional sections
// contributed by modules that declare a `character_panel` jsx in their manifest
// (e.g. wb_core_rpg stats/skills, wb_character_tracker change log).
export default function CharacterView({ isOpen, onClose, modules, gameState, onCommand, busy }) {
  useEffect(() => {
    if (!isOpen) return;
    function onKey(e) { if (e.key === 'Escape') onClose(); }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const player = gameState?.characters?.default_player || {};
  const name = player.name || 'Adventurer';
  const race = player.race || '';
  const gender = player.gender || '';
  const appearance = player.full_appearance || player.short_appearance || '';
  const personality = player.personality || '';
  const identity = [race, gender].filter(Boolean).join(' · ');

  const panelModules = (modules || []).filter((m) => m.character_panel);
  // "Update from story" buttons are backed by the Player Character Tracker's
  // `/character update` command, so they only appear when that module is active.
  const hasTracker = (modules || []).some((m) => m.id === 'wb_character_tracker');

  const sendUpdate = (target) => {
    if (!onCommand || busy) return;
    onCommand(`/character update ${target}`);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.7)' }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-2xl max-h-[88vh] overflow-y-auto shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 bg-gray-900/95 backdrop-blur border-b border-gray-700 px-5 py-3 flex items-center justify-between rounded-t-xl">
          <div className="flex items-center gap-3 min-w-0">
            <h2 className="text-gray-100 font-bold text-lg truncate">{name}</h2>
            {identity && (
              <span className="text-xs px-2 py-0.5 bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 rounded-full capitalize whitespace-nowrap">
                {identity}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-200 text-lg leading-none p-1 transition-colors"
            aria-label="Close character view"
          >
            {'✕'}
          </button>
        </div>

        <div className="p-5 space-y-5">
          {/* --- Core identity --- */}
          {appearance && (
            <section>
              <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Appearance</h3>
              <p className="text-sm text-gray-300 leading-relaxed bg-gray-800/40 rounded-lg p-3 border border-gray-700/50">{appearance}</p>
            </section>
          )}

          {personality && (
            <section>
              <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Personality</h3>
              <p className="text-sm text-gray-300 leading-relaxed bg-gray-800/40 rounded-lg p-3 border border-gray-700/50">{personality}</p>
            </section>
          )}

          {!appearance && !personality && (
            <p className="text-sm text-gray-500 italic">No descriptive details recorded for this character yet.</p>
          )}

          {/* --- Manual "catch the record up with the story" buttons --- */}
          {hasTracker && onCommand && (
            <section>
              <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-2">Update from Story</h3>
              <div className="bg-gray-800/40 rounded-lg p-3 border border-gray-700/50">
                <p className="text-xs text-gray-500 mb-2.5">
                  Ask the AI to rewrite the character record so it reflects everything that has happened so far.
                </p>
                <div className="flex flex-wrap gap-2">
                  {[
                    ['appearance', 'Update Appearance'],
                    ['personality', 'Update Personality'],
                    ['both', 'Update Both'],
                  ].map(([target, label]) => (
                    <button
                      key={target}
                      onClick={() => sendUpdate(target)}
                      disabled={busy}
                      className="text-xs px-3 py-1.5 rounded-lg border border-indigo-500/30 bg-indigo-500/15 text-indigo-300 hover:bg-indigo-500/25 hover:text-indigo-200 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {busy && (
                  <p className="text-xs text-indigo-300/80 mt-2.5 animate-pulse">
                    Updating… the record will refresh when the AI finishes.
                  </p>
                )}
              </div>
            </section>
          )}

          {/* --- Module-contributed panels --- */}
          {panelModules.map((mod) => (
            <section key={mod.id}>
              <h3 className="text-xs uppercase tracking-wider text-gray-500 mb-2">{mod.name || mod.id}</h3>
              <WidgetErrorBoundary modId={mod.id}>
                <DynamicWidget
                  modId={mod.id}
                  entryFile={mod.character_panel}
                  state={gameState}
                  config={gameState?.module_configs?.[mod.id] || {}}
                  slotName="character_panel"
                />
              </WidgetErrorBoundary>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
