const TYPE_COLORS = {
  storyteller:    { bg: 'bg-purple-900/30', text: 'text-purple-300', border: 'border-purple-700/50', icon: '📖' },
  reader:         { bg: 'bg-blue-900/30', text: 'text-blue-300', border: 'border-blue-700/50', icon: '📊' },
  embedding:      { bg: 'bg-gray-800/30', text: 'text-gray-400', border: 'border-gray-700/50', icon: '🧬' },
  librarian:      { bg: 'bg-emerald-900/30', text: 'text-emerald-300', border: 'border-emerald-700/50', icon: '📚' },
  world_build:    { bg: 'bg-orange-900/30', text: 'text-orange-300', border: 'border-orange-700/50', icon: '🌍' },
  character_build:{ bg: 'bg-pink-900/30', text: 'text-pink-300', border: 'border-pink-700/50', icon: '👤' },
  module_fast:    { bg: 'bg-yellow-900/30', text: 'text-yellow-300', border: 'border-yellow-700/50', icon: '⚡' },
  diagnostic:     { bg: 'bg-red-900/30', text: 'text-red-300', border: 'border-red-700/50', icon: '🔧' },
};

// Per-role styling for the split input messages.
const ROLE_COLORS = {
  system:    { text: 'text-amber-300', dot: 'bg-amber-400' },
  user:      { text: 'text-sky-300', dot: 'bg-sky-400' },
  assistant: { text: 'text-emerald-300', dot: 'bg-emerald-400' },
  tool:      { text: 'text-violet-300', dot: 'bg-violet-400' },
};

function roleColors(role) {
  return ROLE_COLORS[role] || { text: 'text-gray-400', dot: 'bg-gray-500' };
}

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
  return `⬆${tIn || 0} ⬇${tOut || 0}`;
}

export default function LLMCallCard({ call, expanded, onToggle }) {
  const colors = TYPE_COLORS[call.call_type] || TYPE_COLORS.storyteller;
  const isRunning = call.status === 'running';
  const isCancelled = call.status === 'cancelled';
  const hasError = !!call.error;

  return (
    <div className={`border ${hasError ? 'border-red-700/80 bg-red-950/20' : colors.border} ${colors.bg} rounded-lg overflow-hidden ${isRunning ? 'ring-1 ring-amber-500/40' : ''}`}>
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
        {isCancelled && <span className="text-[10px] text-gray-400 uppercase">stopped</span>}
        {/* Status / duration */}
        {isRunning ? (
          <span className="flex items-center gap-1 text-[10px] text-amber-300 w-16 justify-end">
            <svg className="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
            </svg>
            running
          </span>
        ) : (
          <>
            <span className="text-[10px] text-gray-500 w-10 text-right">{formatDuration(call.duration_ms)}</span>
            <span className="text-[10px] text-gray-600 w-16 text-right">{formatTokens(call.tokens_in, call.tokens_out)}</span>
          </>
        )}
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
            {isRunning && <span className="text-amber-300">In progress…</span>}
            {isCancelled && <span className="text-gray-400">Stopped by user</span>}
          </div>

          {/* Error */}
          {hasError && (
            <div className="text-xs text-red-400 bg-red-950/40 rounded p-2 border border-red-800/50">
              {call.error}
            </div>
          )}

          {/* Input */}
          <div>
            <div className="text-[10px] text-gray-600 mb-1">Input</div>
            <InputView input={call.full_input} fallback={call.input_summary} />
          </div>

          {/* Output */}
          {!hasError && (
            <div>
              <div className="text-[10px] text-gray-600 mb-1">Output</div>
              {(call.full_output || call.output_summary) ? (
                <pre className="text-[11px] text-gray-300 bg-black/30 rounded p-2 overflow-x-auto max-h-40 whitespace-pre-wrap break-all font-mono leading-relaxed">
                  {call.full_output || call.output_summary}
                </pre>
              ) : (
                <div className="text-[11px] text-gray-500 italic bg-black/30 rounded p-2">
                  {isRunning ? 'Waiting for response…' : isCancelled ? '(stopped before a response arrived)' : '(no output)'}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Renders the input. When it's a list of chat messages, each message is shown
// as its own block labeled with its role; otherwise it falls back to plain text.
function InputView({ input, fallback }) {
  if (Array.isArray(input) && input.length > 0) {
    return (
      <div className="space-y-1.5">
        {input.map((m, i) => {
          const role = (m && m.role) || '?';
          const content = m && typeof m === 'object' ? (m.content ?? '') : String(m);
          const rc = roleColors(role);
          return (
            <div key={i} className="bg-black/30 rounded p-2">
              <div className="flex items-center gap-1.5 mb-1">
                <span className={`w-1.5 h-1.5 rounded-full ${rc.dot}`} />
                <span className={`text-[10px] font-semibold uppercase tracking-wide ${rc.text}`}>{role}</span>
              </div>
              <pre className="text-[11px] text-gray-300 overflow-x-auto max-h-40 whitespace-pre-wrap break-words font-mono leading-relaxed">
                {typeof content === 'string' ? content : JSON.stringify(content, null, 2)}
              </pre>
            </div>
          );
        })}
      </div>
    );
  }

  const text = typeof input === 'string'
    ? input
    : input != null
      ? JSON.stringify(input, null, 2)
      : (fallback || '');

  if (!text) return <div className="text-[11px] text-gray-500 italic bg-black/30 rounded p-2">(no input)</div>;

  return (
    <pre className="text-[11px] text-gray-300 bg-black/30 rounded p-2 overflow-x-auto max-h-40 whitespace-pre-wrap break-all font-mono leading-relaxed">
      {text}
    </pre>
  );
}
