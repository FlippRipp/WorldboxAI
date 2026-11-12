import { useEffect, useState } from 'react';
import { api } from '../../lib/api';
import FirstRunSetup from './FirstRunSetup';

const CORE_MODES = [
  {
    id: 'storyteller-select',
    label: 'Storyteller',
    description: 'Load or create a story and start playing',
    icon: '📜',
  },
  {
    id: 'scenario-manager',
    label: 'Scenarios',
    description: 'Create a simple starting scenario (no world required)',
    icon: '🎬',
  },
  {
    id: 'lorebook-manager',
    label: 'Lorebooks',
    description: 'Import SillyTavern World Info as RAG lore for stories',
    icon: '📚',
  },
  {
    id: 'character-creator',
    label: 'Character Creator',
    description: 'Create a new character with AI assistance',
    icon: '👤',
  },
  {
    id: 'prompt-studio',
    label: 'Prompt Studio',
    description: 'Configure the global AI prompt pipeline',
    icon: '💬',
  },
  {
    id: 'settings',
    label: 'Settings',
    description: 'AI providers, models, and appearance / theme',
    icon: '⚙️',
  },
  {
    id: 'module-manager',
    label: 'Modules',
    description: 'Enable, disable, and install app-wide modules',
    icon: '🧩',
  },
  {
    id: 'server-logs',
    label: 'Server Log',
    description: 'View backend server logs and filter for errors',
    icon: '📋',
  },
  {
    id: 'api-usage',
    label: 'API Usage',
    description: 'Track API spend over time by model and module',
    icon: '💳',
  },
];

export default function MainMenu({ onSelectMode, onContinueSave, modules, onModulesLoaded }) {
  // First-run detection: no provider has an API key (config or .env) and the
  // backend isn't in mock mode -> show the guided setup card. "Skip for now"
  // hides it until the next visit to the menu.
  const [needsSetup, setNeedsSetup] = useState(false);
  const [setupDismissed, setSetupDismissed] = useState(false);
  const [recentSave, setRecentSave] = useState(null);
  const [resumingSave, setResumingSave] = useState(false);

  useEffect(() => {
    if (!modules || modules.length === 0) {
      api.getModules()
        .then(data => onModulesLoaded?.(data.modules || []))
        .catch(() => {});
    }
    api.getHealth()
      .then(health => setNeedsSetup(health.status === 'missing_api_key'))
      .catch(() => {});
    api.getSaves()
      .then((data) => {
        const list = data.saves || [];
        const active = list.find((s) => s.active) || list[0] || null;
        setRecentSave(active);
      })
      .catch(() => {});
  }, []);

  const handleContinue = async () => {
    if (!recentSave || resumingSave) return;
    setResumingSave(true);
    try {
      const data = await api.loadSave(recentSave.id, { ifNeeded: Boolean(recentSave.active) });
      onContinueSave?.(recentSave.id, data?.state, data);
    } catch {
      // Fall back to the save list so the user sees the error context there.
      onSelectMode('storyteller-select');
    } finally {
      setResumingSave(false);
    }
  };

  const moduleModes = (modules || []).flatMap(m =>
    (m.modes || []).map(mode => ({
      id: `module:${m.id}:${mode.id}`,
      label: mode.label || mode.id,
      description: mode.description || `Module: ${m.name}`,
      icon: mode.icon || '🧩',
      moduleId: m.id,
    }))
  );

  const allModes = [...CORE_MODES, ...moduleModes];

  return (
    <div className="min-h-dvh bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center justify-center px-4 py-6 sm:p-6">
      <div className="mb-5 sm:mb-10 text-center">
        <h1 className="text-3xl sm:text-5xl font-bold bg-gradient-to-r from-purple-400 to-pink-400 bg-clip-text text-transparent mb-1.5 sm:mb-3">
          WorldBox
        </h1>
        <p className="text-gray-400 text-xs sm:text-sm tracking-wide">
          AI-driven roleplaying engine
        </p>
      </div>

      {needsSetup && !setupDismissed && (
        <FirstRunSetup
          onDone={() => setNeedsSetup(false)}
          onDismiss={() => setSetupDismissed(true)}
        />
      )}

      {recentSave && onContinueSave && (
        <div className="max-w-4xl w-full mb-3 sm:mb-5">
          <button
            onClick={handleContinue}
            disabled={resumingSave}
            className="w-full flex items-center justify-between gap-3 px-4 py-3.5 sm:px-5 sm:py-4 rounded-xl border border-purple-500/40 bg-gradient-to-r from-purple-900/40 via-gray-800/80 to-gray-800/80 hover:border-purple-400 hover:from-purple-900/60 transition-all shadow-lg shadow-purple-950/20 text-left cursor-pointer disabled:opacity-60"
          >
            <div className="flex items-center gap-3 min-w-0">
              <span className="text-2xl shrink-0">📖</span>
              <div className="min-w-0">
                <div className="text-[11px] uppercase tracking-wider font-semibold text-purple-300">
                  {resumingSave ? 'Loading story…' : 'Continue Story'}
                </div>
                <div className="font-semibold text-gray-100 truncate text-sm sm:text-base">
                  {recentSave.display_name || recentSave.id}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <span className="text-xs text-gray-400 font-mono">Turn {recentSave.turn ?? 0}</span>
              <span className="w-8 h-8 rounded-lg bg-purple-600 text-white flex items-center justify-center">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                </svg>
              </span>
            </div>
          </button>
        </div>
      )}

      <div className="grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-3 gap-2.5 sm:gap-4 max-w-4xl w-full">
        {allModes.map(mode => (
          <button
            key={mode.id}
            onClick={() => {
              if (!mode.placeholder) onSelectMode(mode.id);
            }}
            disabled={mode.placeholder}
            className={`
              group relative flex flex-col items-center justify-center gap-1.5 sm:gap-3 p-3.5 sm:p-6 rounded-xl border
              transition-all duration-200 text-center
              ${mode.placeholder
                ? 'border-gray-800 bg-gray-900/50 opacity-50 cursor-not-allowed'
                : 'border-gray-700 bg-gray-800/70 hover:bg-gray-800 hover:border-purple-500/50 hover:shadow-lg hover:shadow-purple-500/10 cursor-pointer'
              }
            `}
          >
            {mode.placeholder && (
              <span className="absolute top-2 right-2 text-[10px] px-1.5 py-0.5 rounded bg-gray-700 text-gray-400 font-medium">
                SOON
              </span>
            )}
            <span className="text-2xl sm:text-3xl group-hover:scale-110 transition-transform duration-200">
              {mode.icon}
            </span>
            <div className="min-w-0 w-full">
              <h3 className="font-semibold text-gray-100 text-xs sm:text-base leading-snug mb-0.5 sm:mb-1 truncate">
                {mode.label}
              </h3>
              <p className="text-[11px] sm:text-xs text-gray-500 leading-snug line-clamp-2">
                {mode.description}
              </p>
            </div>
          </button>
        ))}
      </div>

      {allModes.length === 0 && (
        <p className="text-gray-500 mt-8">No modes available. Load some modules to get started.</p>
      )}
    </div>
  );
}
