import { useEffect, useState } from 'react';
import { api } from '../../lib/api';

// Debug log tools: download the persistent LLM call log (every call the
// server ever made, as JSONL), and download a selected save's full state
// as a JSON dump.
export default function LogSettings() {
  const [saves, setSaves] = useState(null);
  const [selectedSave, setSelectedSave] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    api.getSaves()
      .then((data) => {
        if (cancelled) return;
        const list = data?.saves || [];
        setSaves(list);
        if (list.length > 0) setSelectedSave(list[0].id);
      })
      .catch((e) => { if (!cancelled) { setSaves([]); setError(e.message || 'Failed to load saves.'); } });
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-4 space-y-6">
      <div className="flex items-center justify-between gap-4">
        <div className="flex flex-col">
          <span className="text-sm font-medium text-gray-300">LLM call log</span>
          <span className="text-xs text-gray-500">Every LLM call the server has made, with full input and output, as JSONL.</span>
        </div>
        <a
          href={api.llmLogDumpUrl()}
          download
          className="shrink-0 px-3 py-1.5 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium transition-colors"
        >
          Dump LLM Log
        </a>
      </div>

      <div className="border-t border-gray-800 pt-4 space-y-2">
        <div className="flex flex-col">
          <span className="text-sm font-medium text-gray-300">Dump save state</span>
          <span className="text-xs text-gray-500">Download the selected save's full state as a JSON file.</span>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={selectedSave}
            onChange={(e) => setSelectedSave(e.target.value)}
            disabled={!saves || saves.length === 0}
            className="flex-1 min-w-0 bg-gray-700 border border-gray-600 rounded px-3 py-1.5 text-gray-200 text-sm disabled:opacity-40"
            aria-label="Save to dump"
          >
            {saves === null && <option value="">Loading saves…</option>}
            {saves !== null && saves.length === 0 && <option value="">No saves available</option>}
            {(saves || []).map((s) => (
              <option key={s.id} value={s.id}>{s.display_name || s.id}</option>
            ))}
          </select>
          {selectedSave ? (
            <a
              href={api.saveDumpUrl(selectedSave)}
              download
              className="shrink-0 px-3 py-1.5 rounded-lg bg-purple-600 hover:bg-purple-500 text-white text-sm font-medium transition-colors"
            >
              Download Dump
            </a>
          ) : (
            <span className="shrink-0 px-3 py-1.5 rounded-lg bg-purple-600 text-white text-sm font-medium opacity-40 cursor-not-allowed">
              Download Dump
            </span>
          )}
        </div>
      </div>

      {error && <div className="text-xs text-red-400">{error}</div>}
    </div>
  );
}
