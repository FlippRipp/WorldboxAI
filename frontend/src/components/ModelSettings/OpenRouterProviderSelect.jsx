import { useEffect, useRef, useState } from 'react';

const SELECT_CLASS = 'w-full min-w-0 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-gray-200 text-sm focus:border-purple-500 focus:outline-none disabled:opacity-50';

export default function OpenRouterProviderSelect({ label, modelValue, value, onChange, fetchProviders, fallback = false, className = '' }) {
  const modelIds = [...new Set((fallback ? modelValue.split(',') : [modelValue])
    .map((model) => model.trim()).filter(Boolean))];
  const requestKey = JSON.stringify(modelIds);
  const hasModels = modelIds.length > 0;
  const [attempt, setAttempt] = useState(0);
  const refreshRequested = useRef(false);
  const [result, setResult] = useState(null);

  useEffect(() => {
    const requestedModels = JSON.parse(requestKey);
    if (!requestedModels.length) return;
    let cancelled = false;
    const refresh = refreshRequested.current;
    refreshRequested.current = false;
    const timer = setTimeout(async () => {
      const responses = await Promise.allSettled(requestedModels.map((model) => fetchProviders(model, refresh)));
      if (cancelled) return;
      const providers = new Map();
      const errors = [];
      responses.forEach((response, index) => {
        if (response.status === 'rejected') {
          errors.push(`${requestedModels[index]}: ${response.reason?.message || 'Unable to load providers.'}`);
        } else {
          for (const provider of response.value) providers.set(provider.id, provider);
        }
      });
      setResult({
        requestKey,
        attempt,
        providers: [...providers.values()].sort((a, b) => a.label.localeCompare(b.label)),
        error: errors.join(' '),
      });
    }, fallback ? 250 : 0);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [requestKey, attempt, fetchProviders, fallback]);

  // Hide prior options immediately when the model or refresh attempt changes.
  const current = result?.requestKey === requestKey && result.attempt === attempt ? result : null;
  const loading = hasModels && !current;
  const providers = current?.providers || [];
  const error = current?.error || '';
  const missingSelection = value && !providers.some((provider) => provider.id === value);
  const savedUnavailable = hasModels && !loading && !error && missingSelection;

  const refresh = () => {
    refreshRequested.current = true;
    setAttempt((previous) => previous + 1);
  };

  return (
    <div className={className}>
      <div className="flex items-start gap-2">
        <select
          aria-label={label}
          aria-busy={loading}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          disabled={!hasModels || loading}
          className={SELECT_CLASS}
        >
          <option value="">Any provider (let OpenRouter decide)</option>
          {missingSelection && <option value={value}>Saved provider: {value}</option>}
          {providers.map((provider) => (
            <option key={provider.id} value={provider.id}>{provider.label}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={refresh}
          disabled={!hasModels || loading}
          aria-label={`${error ? 'Retry' : 'Refresh'} ${label}`}
          className="shrink-0 rounded px-2 py-2 text-xs text-purple-300 hover:bg-gray-700 disabled:opacity-40"
        >
          {error ? 'Retry' : 'Refresh'}
        </button>
      </div>
      <div aria-live="polite" className="mt-1 space-y-1 text-xs">
        {!hasModels && <p className="text-gray-400">Choose a model to see its providers.</p>}
        {loading && <p className="text-gray-400">Fetching providers...</p>}
        {error && <p className="text-red-400">Could not load all providers. {error}</p>}
        {savedUnavailable && (
          <p className="text-amber-300">Saved provider “{value}” is not listed for the selected {fallback ? 'models' : 'model'}. Your selection has been kept.</p>
        )}
        {hasModels && !loading && !error && providers.length === 0 && (
          <p className="text-gray-400">No providers are currently listed for the selected {fallback ? 'models' : 'model'}.</p>
        )}
        {fallback && hasModels && <p className="text-gray-400">Providers serving at least one fallback model.</p>}
      </div>
    </div>
  );
}
