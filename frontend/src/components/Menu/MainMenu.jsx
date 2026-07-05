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
];

export default function MainMenu({ onSelectMode, modules, onModulesLoaded }) {
  // First-run detection: no provider has an API key (config or .env) and the
  // backend isn't in mock mode -> show the guided setup card. "Skip for now"
  // hides it until the next visit to the menu.
  const [needsSetup, setNeedsSetup] = useState(false);
  const [setupDismissed, setSetupDismissed] = useState(false);

  useEffect(() => {
    api.getModules()
      .then(data => onModulesLoaded?.(data.modules || []))
      .catch(() => {});
    api.getHealth()
      .then(health => setNeedsSetup(health.status === 'missing_api_key'))
      .catch(() => {});
  }, []);

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
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center justify-center p-6">
      <div className="mb-12 text-center">
        <h1 className="text-5xl font-bold bg-gradient-to-r from-purple-400 to-pink-400 bg-clip-text text-transparent mb-3">
          WorldBox
        </h1>
        <p className="text-gray-400 text-sm tracking-wide">
          AI-driven roleplaying engine
        </p>
      </div>

      {needsSetup && !setupDismissed && (
        <FirstRunSetup
          onDone={() => setNeedsSetup(false)}
          onDismiss={() => setSetupDismissed(true)}
        />
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 max-w-4xl w-full">
        {allModes.map(mode => (
          <button
            key={mode.id}
            onClick={() => {
              if (!mode.placeholder) onSelectMode(mode.id);
            }}
            disabled={mode.placeholder}
            className={`
              group relative flex flex-col items-center gap-3 p-6 rounded-xl border
              transition-all duration-200 text-left
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
            <span className="text-3xl group-hover:scale-110 transition-transform duration-200">
              {mode.icon}
            </span>
            <div className="text-center">
              <h3 className="font-semibold text-gray-100 mb-1">{mode.label}</h3>
              <p className="text-xs text-gray-500 leading-relaxed">{mode.description}</p>
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
