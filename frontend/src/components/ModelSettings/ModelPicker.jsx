import { useState, useRef, useEffect } from 'react';

export default function ModelPicker({ models, value, onChange, providerPrefix }) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const ref = useRef(null);

  useEffect(() => {
    function handleClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    if (open) document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  const displayModels = (() => {
    if (!providerPrefix) return models;
    const prefix = providerPrefix.endsWith('/') ? providerPrefix : providerPrefix + '/';
    const result = [];
    const seen = new Set();
    for (const m of models) {
      if (m.startsWith(prefix)) {
        const name = m.slice(prefix.length);
        if (!seen.has(name)) {
          seen.add(name);
          result.push(name);
        }
      }
    }
    return result;
  })();

  const filtered = displayModels.filter(m => m.toLowerCase().includes(search.toLowerCase()));

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => { setOpen(!open); setSearch(''); }}
        className="px-2 py-2 bg-gray-700 hover:bg-gray-600 rounded text-gray-400 text-xs transition-colors whitespace-nowrap flex items-center gap-1"
        title={providerPrefix ? `Models for ${providerPrefix}` : 'Pick from available models'}
      >
        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M19 9l-7 7-7-7" />
        </svg>
        <span className="hidden sm:inline">Models</span>
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 w-80 bg-gray-800 border border-gray-700 rounded-lg shadow-2xl z-50 max-h-64 flex flex-col">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search models..."
            className="w-full bg-gray-900 border-b border-gray-700 px-3 py-2 text-gray-200 text-sm focus:outline-none rounded-t-lg"
            autoFocus
          />
          <div className="overflow-y-auto">
            {filtered.slice(0, 100).map((m) => (
              <button
                key={m}
                onClick={() => { onChange(m); setOpen(false); }}
                className={`w-full text-left px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700 transition-colors font-mono ${m === value ? 'text-purple-400 bg-gray-700/50' : ''}`}
              >
                {m}
              </button>
            ))}
            {displayModels.length === 0 && providerPrefix && (
              <div className="px-3 py-2 text-sm text-gray-500">No models for this provider. Check API key and click Fetch.</div>
            )}
            {filtered.length === 0 && displayModels.length > 0 && (
              <div className="px-3 py-2 text-sm text-gray-500">No matches</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
