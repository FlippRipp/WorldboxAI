import { useState, useEffect } from 'react';
import { api } from '../../lib/api';

export default function HealthPanel({ isOpen, onClose }) {
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (isOpen) {
      setLoading(true);
      setError('');
      api.getHealth()
        .then(data => setHealth(data))
        .catch(e => setError(e.message))
        .finally(() => setLoading(false));
    }
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" role="dialog" aria-modal="true">
      <div className="bg-gray-800 w-full max-w-lg rounded-lg shadow-2xl border border-gray-700 flex flex-col max-h-[80vh]">
        <div className="p-4 border-b border-gray-700 flex justify-between items-center bg-gray-900 rounded-t-lg">
          <h2 className="text-xl font-bold text-gray-100">System Health</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-2xl leading-none">&times;</button>
        </div>

        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {loading && <div className="text-center text-gray-400 py-8">Loading...</div>}
          {error && <div className="bg-red-900/40 border border-red-700/50 rounded p-3 text-red-200 text-sm">{error}</div>}

          {health && (
            <>
              <Section title="Providers">
                <StatusRow label="LLM Mode" value={health.llm?.LLM_MODE || 'unknown'} status={health.llm?.LLM_MODE === 'mock' ? 'warn' : 'ok'} />
                <StatusRow label="Gemini Key" value={health.llm?.GEMINI_API_KEY ? 'Found' : 'Missing'} status={health.llm?.GEMINI_API_KEY ? 'ok' : 'error'} />
                <StatusRow label="OpenRouter Key" value={health.llm?.OPENROUTER_API_KEY ? 'Found' : 'Missing'} status={health.llm?.OPENROUTER_API_KEY ? 'ok' : 'error'} />
                <StatusRow label="OpenAI Key" value={health.llm?.OPENAI_API_KEY ? 'Found' : 'Missing'} status={health.llm?.OPENAI_API_KEY ? 'ok' : 'error'} />
                <StatusRow label="Anthropic Key" value={health.llm?.ANTHROPIC_API_KEY ? 'Found' : 'Missing'} status={health.llm?.ANTHROPIC_API_KEY ? 'ok' : 'error'} />
              </Section>

              <Section title="Models">
                <StatusRow label="Storyteller" value={health.llm?.STORYTELLER_MODEL || 'N/A'} />
                <StatusRow label="Fallbacks" value={health.llm?.STORYTELLER_FALLBACK_MODELS?.join(', ') || 'none'} />
                <StatusRow label="Reader" value={health.llm?.READER_MODEL || 'N/A'} />
                <StatusRow label="Embedding" value={health.llm?.EMBEDDING_MODEL || 'N/A'} />
                <StatusRow label="Retries" value={health.llm?.LLM_PROVIDER_RETRY_ATTEMPTS || 'N/A'} />
              </Section>

              <Section title="Session">
                <StatusRow label="Active Save" value={health.session?.active_save_id || 'none'} />
                <StatusRow label="Current Turn" value={health.session?.turn ?? 0} />
              </Section>

              <Section title="Modules">
                {(health.modules || []).map((m) => (
                  <StatusRow key={m.id} label={m.name || m.id} value={`v${m.version}`} status="ok" />
                ))}
                {(!health.modules || health.modules.length === 0) && (
                  <div className="text-gray-500 text-sm italic">No modules loaded</div>
                )}
              </Section>

              <Section title="Memory">
                <StatusRow label="Status" value={health.memory?.status || 'unknown'} />
                <StatusRow label="Entries" value={health.memory?.entry_count ?? 'N/A'} />
                {health.memory?.embedding_model && (
                  <StatusRow label="Embedding Model" value={health.memory.embedding_model} />
                )}
              </Section>
            </>
          )}
        </div>

        <div className="p-4 border-t border-gray-700 bg-gray-900 rounded-b-lg flex justify-end">
          <button onClick={onClose} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded font-medium transition-colors">
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="bg-gray-900/50 rounded border border-gray-700 p-4">
      <h3 className="text-sm font-semibold uppercase tracking-wide text-purple-300 mb-3">{title}</h3>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function StatusRow({ label, value, status }) {
  const statusColors = {
    ok: 'text-green-400',
    warn: 'text-yellow-400',
    error: 'text-red-400',
  };

  return (
    <div className="flex justify-between items-center text-sm">
      <span className="text-gray-400">{label}</span>
      <span className={`font-mono ${status ? statusColors[status] || 'text-gray-300' : 'text-gray-300'}`}>
        {status && <span className="mr-2 inline-block w-2 h-2 rounded-full bg-current opacity-75" />}
        {value}
      </span>
    </div>
  );
}
