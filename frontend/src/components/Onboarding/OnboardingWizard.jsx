import { useState } from 'react';
import FirstRunSetup from '../Menu/FirstRunSetup';

const FEATURES = [
  {
    icon: '📖',
    title: 'AI Storyteller',
    text: 'Type what your character does — the AI narrates the outcome, streaming the story as it writes.',
  },
  {
    icon: '🧠',
    title: 'Living world',
    text: 'The engine remembers what happened, tracks your stats and inventory, and keeps the world consistent across sessions.',
  },
  {
    icon: '🧩',
    title: 'Modular systems',
    text: 'RPG stats, world generation, NPCs and more are modules — enable exactly the systems you want per story.',
  },
];

const NEXT_STEPS = [
  { icon: '📜', title: 'Storyteller', text: 'Create a story and start playing — a blank canvas works fine.' },
  { icon: '👤', title: 'Character Creator', text: 'Build a character with AI assistance to play as.' },
  { icon: '🌍', title: 'World Generation', text: 'Generate a whole world first, then set your story in it.' },
];

function StepDots({ step, count }) {
  return (
    <div className="flex items-center justify-center gap-2" aria-label={`Step ${step + 1} of ${count}`}>
      {Array.from({ length: count }, (_, i) => (
        <span
          key={i}
          className={`h-1.5 rounded-full transition-all duration-300 ${
            i === step ? 'w-6 bg-purple-400' : 'w-1.5 bg-gray-700'
          }`}
        />
      ))}
    </div>
  );
}

// Full-screen wizard shown once on a fresh install (no AI provider configured
// anywhere). Welcome -> connect a provider -> what to do next. Finishing or
// skipping marks onboarding done; a skipped key setup still gets the reminder
// card on the main menu.
export default function OnboardingWizard({ onFinish }) {
  const [step, setStep] = useState(0);
  // Whether step 2 actually connected a provider — the last step's wording
  // and actions adapt (can't start a story without a key).
  const [connected, setConnected] = useState(false);

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-2xl flex flex-col items-center">

        {step === 0 && (
          <>
            <h1 className="text-5xl font-bold bg-gradient-to-r from-purple-400 to-pink-400 bg-clip-text text-transparent mb-3 text-center">
              Welcome to WorldBox
            </h1>
            <p className="text-gray-400 mb-10 text-center max-w-md">
              An AI-driven roleplaying engine. You play, it narrates — and remembers.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 w-full mb-10">
              {FEATURES.map((f) => (
                <div key={f.title} className="p-5 rounded-xl border border-gray-700 bg-gray-800/70 text-center">
                  <div className="text-3xl mb-3">{f.icon}</div>
                  <h3 className="font-semibold text-gray-100 mb-1.5 text-sm">{f.title}</h3>
                  <p className="text-xs text-gray-500 leading-relaxed">{f.text}</p>
                </div>
              ))}
            </div>
            <button
              onClick={() => setStep(1)}
              className="px-8 py-3 rounded-xl bg-purple-600 hover:bg-purple-500 text-white font-medium transition-colors shadow-lg shadow-purple-500/20"
            >
              Get started
            </button>
          </>
        )}

        {step === 1 && (
          <>
            <h1 className="text-3xl font-bold text-gray-100 mb-3 text-center">
              Connect your AI
            </h1>
            <p className="text-gray-400 mb-8 text-center max-w-md">
              One API key powers the whole engine. Gemini's free tier is plenty to start with.
            </p>
            <FirstRunSetup
              onDone={() => { setConnected(true); setStep(2); }}
              onDismiss={() => setStep(2)}
            />
            <button
              onClick={() => setStep(0)}
              className="text-sm text-gray-500 hover:text-gray-300 transition-colors"
            >
              ← Back
            </button>
          </>
        )}

        {step === 2 && (
          <>
            <h1 className="text-3xl font-bold text-gray-100 mb-3 text-center">
              {connected ? "You're all set!" : 'Almost there'}
            </h1>
            <p className="text-gray-400 mb-8 text-center max-w-md">
              {connected
                ? 'Here are a few good places to start.'
                : 'You skipped the AI setup — you can add a key any time from the main menu or Settings. Until then, stories can\'t generate.'}
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 w-full mb-10">
              {NEXT_STEPS.map((s) => (
                <div key={s.title} className="p-5 rounded-xl border border-gray-700 bg-gray-800/70 text-center">
                  <div className="text-3xl mb-3">{s.icon}</div>
                  <h3 className="font-semibold text-gray-100 mb-1.5 text-sm">{s.title}</h3>
                  <p className="text-xs text-gray-500 leading-relaxed">{s.text}</p>
                </div>
              ))}
            </div>
            <div className="flex flex-col sm:flex-row items-center gap-4">
              {connected && (
                <button
                  onClick={() => onFinish?.('storyteller-select')}
                  className="px-8 py-3 rounded-xl bg-purple-600 hover:bg-purple-500 text-white font-medium transition-colors shadow-lg shadow-purple-500/20"
                >
                  Start your first story
                </button>
              )}
              <button
                onClick={() => onFinish?.(null)}
                className={connected
                  ? 'text-sm text-gray-500 hover:text-gray-300 transition-colors'
                  : 'px-8 py-3 rounded-xl bg-purple-600 hover:bg-purple-500 text-white font-medium transition-colors shadow-lg shadow-purple-500/20'}
              >
                Go to main menu
              </button>
            </div>
          </>
        )}

        <div className="mt-12">
          <StepDots step={step} count={3} />
        </div>
      </div>
    </div>
  );
}
