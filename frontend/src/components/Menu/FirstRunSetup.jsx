import { useState, useEffect } from 'react';
import { api } from '../../lib/api';

// Where to get a key, per provider. Shown as a help link under the input.
const KEY_URLS = {
  gemini: { label: 'Google AI Studio (free tier available)', url: 'https://aistudio.google.com/apikey' },
  openrouter: { label: 'openrouter.ai/keys', url: 'https://openrouter.ai/keys' },
  openai: { label: 'platform.openai.com/api-keys', url: 'https://platform.openai.com/api-keys' },
  deepseek: { label: 'platform.deepseek.com', url: 'https://platform.deepseek.com/api_keys' },
  anthropic: { label: 'console.anthropic.com', url: 'https://console.anthropic.com/settings/keys' },
};

// Shown on the main menu when no AI provider has a key yet (fresh install).
// Saves the key, activates the provider, and proves it works with a live test
// call — so the first failure a new user sees is here, not mid-story.
export default function FirstRunSetup({ onDone, onDismiss }) {
  const [providers, setProviders] = useState([]);
  const [providerId, setProviderId] = useState('gemini');
  const [apiKey, setApiKey] = useState('');
  // 'form' -> 'testing' -> 'success' | back to 'form' with `error` set.
  const [phase, setPhase] = useState('form');
  const [error, setError] = useState(null);
  const [testedModel, setTestedModel] = useState('');

  useEffect(() => {
    api.getProviders()
      .then((data) => {
        const list = data.providers || [];
        setProviders(list);
        const active = list.find((p) => p.active);
        if (active) setProviderId(active.id);
      })
      .catch(() => {});
  }, []);

  const handleConnect = async () => {
    const key = apiKey.trim();
    if (!key) {
      setError('Paste an API key first.');
      return;
    }
    setPhase('testing');
    setError(null);
    try {
      await api.updateProviderConfig(providerId, { api_key: key });
      await api.setActiveProvider(providerId);
      const result = await api.testProvider(providerId);
      if (result.success) {
        setTestedModel(result.model || '');
        setPhase('success');
        setTimeout(() => onDone?.(), 1500);
      } else {
        setPhase('form');
        setError(result.error || 'The provider rejected the request.');
      }
    } catch (e) {
      setPhase('form');
      setError(e.message || 'Could not reach the server.');
    }
  };

  const keyHelp = KEY_URLS[providerId];

  if (phase === 'success') {
    return (
      <div className="w-full max-w-xl mb-10 p-6 rounded-xl border border-green-700/60 bg-green-950/30 text-center">
        <div className="text-2xl mb-2">✅</div>
        <h2 className="font-semibold text-green-200 mb-1">You're connected!</h2>
        <p className="text-sm text-green-300/80">
          {testedModel ? `${testedModel.split('/').pop()} responded.` : 'The AI responded.'}{' '}
          Ready to start your first story.
        </p>
      </div>
    );
  }

  return (
    <div className="w-full max-w-xl mb-10 p-6 rounded-xl border border-purple-600/40 bg-gray-800/80 shadow-lg shadow-purple-500/5">
      <h2 className="font-semibold text-gray-100 mb-1">Set up your AI provider</h2>
      <p className="text-sm text-gray-400 mb-4">
        WorldBox needs an AI provider to tell stories. Paste an API key and we'll
        test it with a live call before you start playing.
      </p>

      <div className="flex flex-col sm:flex-row gap-3 mb-2">
        <select
          value={providerId}
          onChange={(e) => { setProviderId(e.target.value); setError(null); }}
          disabled={phase === 'testing'}
          aria-label="AI provider"
          className="sm:w-44 bg-gray-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-purple-500 focus:outline-none disabled:opacity-50"
        >
          {(providers.length ? providers : [{ id: 'gemini', label: 'Google Gemini' }]).map((p) => (
            <option key={p.id} value={p.id}>{p.label}</option>
          ))}
        </select>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => { setApiKey(e.target.value); setError(null); }}
          onKeyDown={(e) => { if (e.key === 'Enter') handleConnect(); }}
          disabled={phase === 'testing'}
          placeholder="Paste your API key"
          aria-label="API key"
          className="flex-1 bg-gray-900 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:border-purple-500 focus:outline-none disabled:opacity-50"
        />
      </div>

      {keyHelp && (
        <p className="text-xs text-gray-500 mb-3">
          No key yet? Get one at{' '}
          <a href={keyHelp.url} target="_blank" rel="noreferrer" className="text-purple-400 hover:text-purple-300 underline">
            {keyHelp.label}
          </a>
        </p>
      )}

      {error && (
        <p className="text-sm text-red-300 bg-red-950/40 border border-red-900/60 rounded-lg px-3 py-2 mb-3 break-words">
          {error}
        </p>
      )}

      <div className="flex items-center gap-3">
        <button
          onClick={handleConnect}
          disabled={phase === 'testing'}
          className="px-4 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-60 text-white text-sm font-medium transition-colors"
        >
          {phase === 'testing' ? 'Testing connection…' : 'Connect & test'}
        </button>
        {phase === 'testing' && (
          <span className="w-4 h-4 border-2 border-purple-400 border-t-transparent rounded-full animate-spin" aria-hidden="true" />
        )}
        <button
          onClick={() => onDismiss?.()}
          disabled={phase === 'testing'}
          className="ml-auto text-sm text-gray-500 hover:text-gray-300 transition-colors"
        >
          Skip for now
        </button>
      </div>
    </div>
  );
}
