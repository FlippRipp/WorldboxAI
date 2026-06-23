const TYPE_COLORS = {
  storyteller:    { bg: 'bg-purple-900/30', text: 'text-purple-300', border: 'border-purple-700/50', icon: '\uD83D\uDCD6' },
  reader:         { bg: 'bg-blue-900/30', text: 'text-blue-300', border: 'border-blue-700/50', icon: '\uD83D\uDCCA' },
  embedding:      { bg: 'bg-gray-800/30', text: 'text-gray-400', border: 'border-gray-700/50', icon: '\uD83E\uDDEC' },
  librarian:      { bg: 'bg-emerald-900/30', text: 'text-emerald-300', border: 'border-emerald-700/50', icon: '\uD83D\uDCDA' },
  world_build:    { bg: 'bg-orange-900/30', text: 'text-orange-300', border: 'border-orange-700/50', icon: '\uD83C\uDF0D' },
  character_build:{ bg: 'bg-pink-900/30', text: 'text-pink-300', border: 'border-pink-700/50', icon: '\uD83D\uDC64' },
  module_fast:    { bg: 'bg-yellow-900/30', text: 'text-yellow-300', border: 'border-yellow-700/50', icon: '\u26A1' },
  diagnostic:     { bg: 'bg-red-900/30', text: 'text-red-300', border: 'border-red-700/50', icon: '\uD83D\uDD27' },
};

function formatDuration(ms) {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatTimestamp(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatTokens(tIn, tOut) {
  if (!tIn && !tOut) return '';
  return `\u2B06${tIn || 0} \u2B07${tOut || 0}`;
}

export default function LLMCallCard({ call, expanded, onToggle }) {
  const colors = TYPE_COLORS[call.call_type] || TYPE_COLORS.storyteller;
  const hasError = !!call.error;
  const hasFullData = call.full_input != null || call.full_output;

  return (
    <div className={`border ${hasError ? 'border-red-700/80 bg-red-950/20' : colors.border} ${colors.bg} rounded-lg overflow-hidden`}>
      {/* Collapsed row */}
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-white/5 transition-colors"
      >
        <span className="text-sm">{colors.icon}</span>
        <span className={`text-xs font-medium ${colors.text} min-w-0`}>{call.call_label}</span>
        <span className="text-xs text-gray-500 truncate flex-1">{call.model}</span>
        {call.module_source && (
          <span className="text-[10px] bg-gray-800 px-1.5 py-0.5 rounded text-gray-400">
            {call.module_source}
          </span>
        )}
        {hasError && <span className="text-xs text-red-400">ERR</span>}
        <span className="text-[10px] text-gray-500 w-10 text-right">{formatDuration(call.duration_ms)}</span>
        <span className="text-[10px] text-gray-600 w-16 text-right">{formatTokens(call.tokens_in, call.tokens_out)}</span>
        <svg className={`w-3 h-3 text-gray-500 transition-transform ${expanded ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Expanded detail */}
      {expanded && (
        <div className="px-3 pb-3 space-y-2 border-t border-white/5">
          {/* Meta */}
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-gray-500 pt-2">
            <span>ID: {call.id}</span>
            <span>Time: {formatTimestamp(call.timestamp)}</span>
            <span>Step: {call.step}</span>
            <span>Model: {call.model}</span>
            {call.streaming && <span className="text-purple-400">Streaming</span>}
          </div>

          {/* Error */}
          {hasError && (
            <div className="text-xs text-red-400 bg-red-950/40 rounded p-2 border border-red-800/50">
              {call.error}
            </div>
          )}

          {/* Input */}
          {call.input_summary && (
            <div>
              <div className="text-[10px] text-gray-600 mb-1">Input</div>
              <pre className="text-[11px] text-gray-300 bg-black/30 rounded p-2 overflow-x-auto max-h-40 whitespace-pre-wrap break-all font-mono leading-relaxed">
                {expanded && hasFullData ? renderInput(call.full_input) : call.input_summary}
              </pre>
            </div>
          )}

          {/* Output */}
          {(call.output_summary || call.full_output) && !hasError && (
            <div>
              <div className="text-[10px] text-gray-600 mb-1">Output</div>
              <pre className="text-[11px] text-gray-300 bg-black/30 rounded p-2 overflow-x-auto max-h-40 whitespace-pre-wrap break-all font-mono leading-relaxed">
                {expanded && call.full_output ? call.full_output : call.output_summary}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function renderInput(input) {
  if (!input) return '';
  if (typeof input === 'string') return input;
  if (Array.isArray(input)) {
    return input.map(m => {
      if (typeof m === 'object' && m !== null) {
        return `[${m.role || '?'}] ${m.content || ''}`;
      }
      return String(m);
    }).join('\n');
  }
  return JSON.stringify(input, null, 2);
}
