import { useEffect, useState } from 'react';
import WidgetErrorBoundary from '../shared/WidgetErrorBoundary';
import DynamicWidget from '../shared/DynamicWidget';

// Unified character view for the storyteller. Shows the story character's core
// identity (from state.characters.default_player) plus any additional sections
// contributed by modules that declare a `character_panel` jsx in their manifest
// (e.g. wb_core_rpg stats/skills, wb_character_tracker change log). Modules can
// also contribute whole tabs next to the player tab via `character_tab`
// (e.g. wb_npc_system's character browser).
export default function CharacterView({ isOpen, onClose, modules, gameState, onCommand, busy }) {
  const [tab, setTab] = useState('player');

  useEffect(() => {
    if (!isOpen) return;
    function onKey(e) { if (e.key === 'Escape') onClose(); }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const tabModules = (modules || []).filter((m) => m.character_tab);
  const activeTab = tabModules.some((m) => m.id === tab) ? tab : 'player';
  const activeTabModule = tabModules.find((m) => m.id === activeTab);

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
        className="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-3xl max-h-[88vh] overflow-y-auto shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 bg-gray-900/95 backdrop-blur border-b border-gray-700 px-5 pt-3 rounded-t-xl">
          <div className="flex items-center justify-between pb-3">
            <div className="flex items-center gap-3 min-w-0">
              <h2 className="text-gray-100 font-bold text-lg truncate">
                {activeTab === 'player' ? name : (activeTabModule?.character_tab_label || activeTabModule?.name)}
              </h2>
              {activeTab === 'player' && identity && (
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

          {tabModules.length > 0 && (
            <div className="flex gap-1 -mb-px">
              {[{ id: 'player', label: 'Player' }, ...tabModules.map((m) => ({ id: m.id, label: m.character_tab_label || m.name }))].map((t) => (
                <button
                  key={t.id}
                  onClick={() => setTab(t.id)}
                  className={`px-3 py-1.5 text-sm rounded-t-lg border-b-2 transition-colors ${
                    activeTab === t.id
                      ? 'border-indigo-400 text-indigo-300'
                      : 'border-transparent text-gray-500 hover:text-gray-300'
                  }`}
                >
                  {t.label}
                </button>
              ))}
            </div>
          )}
        </div>

        {activeTabModule && (
          <div className="p-5">
            <WidgetErrorBoundary modId={activeTabModule.id}>
              <DynamicWidget
                modId={activeTabModule.id}
                entryFile={activeTabModule.character_tab}
                state={gameState}
                config={gameState?.module_configs?.[activeTabModule.id] || {}}
                slotName="character_tab"
                slotProps={{ onCommand, busy }}
              />
            </WidgetErrorBoundary>
          </div>
        )}

        <div className={activeTab === 'player' ? 'p-5 space-y-5' : 'hidden'}>
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
