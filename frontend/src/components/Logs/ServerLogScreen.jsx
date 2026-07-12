import { useEffect, useRef, useState } from 'react';
import { api } from '../../lib/api';

const ERROR_LEVELS = new Set(['ERROR', 'CRITICAL']);

const LEVEL_BADGES = {
  ERROR:    'bg-red-900/60 text-red-300',
  CRITICAL: 'bg-red-900/60 text-red-300',
  WARNING:  'bg-yellow-900/60 text-yellow-300',
  INFO:     'bg-gray-700 text-gray-300',
  DEBUG:    'bg-gray-800 text-gray-500',
};

const LEVEL_TEXT = {
  ERROR:    'text-red-300',
  CRITICAL: 'text-red-300',
  WARNING:  'text-yellow-200',
  INFO:     'text-gray-300',
  DEBUG:    'text-gray-500',
};

// Keep the client-side buffer in line with the server's ring buffer size.
const MAX_CLIENT_LOGS = 2000;
const POLL_MS = 2000;

function formatTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

export default function ServerLogScreen({ onBack }) {
  const [logs, setLogs] = useState([]);
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [loading, setLoading] = useState(true);
  const scrollRef = useRef(null);
  // Follow new entries only while the user is scrolled to the bottom.
  const stickToBottomRef = useRef(true);
  const lastIdRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const data = await api.getServerLogs(lastIdRef.current);
        if (cancelled) return;
        const fresh = data.logs || [];
        if (fresh.length > 0) {
          lastIdRef.current = fresh[fresh.length - 1].id;
          setLogs(prev => [...prev, ...fresh].slice(-MAX_CLIENT_LOGS));
        }
      } catch {
        // Backend unreachable; retry on the next tick.
      }
      if (!cancelled) setLoading(false);
    };
    poll();
    const interval = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [logs, errorsOnly, loading]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  const handleClear = async () => {
    try {
      await api.clearServerLogs();
      setLogs([]);
    } catch {
      // Keep what we have if the request fails.
    }
  };

  const errorCount = logs.filter(l => ERROR_LEVELS.has(l.level)).length;
  const visible = errorsOnly ? logs.filter(l => ERROR_LEVELS.has(l.level)) : logs;

  const filterBtn = (active) =>
    `px-3 py-1.5 rounded-lg text-sm transition-colors ${
      active ? 'bg-purple-700 text-white' : 'bg-gray-700 hover:bg-gray-600 text-gray-300'
    }`;

  return (
    <div className="h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center p-6">
      <div className="w-full max-w-5xl flex flex-col flex-1 min-h-0">
        <button
          onClick={onBack}
          className="self-start flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-8"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Menu
        </button>

        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <h1 className="text-3xl font-bold text-gray-100">Server Log</h1>
          <div className="flex items-center gap-2">
            <button onClick={() => setErrorsOnly(false)} className={filterBtn(!errorsOnly)}>
              All
            </button>
            <button onClick={() => setErrorsOnly(true)} className={filterBtn(errorsOnly)}>
              Errors{errorCount > 0 ? ` (${errorCount})` : ''}
            </button>
            <button
              onClick={handleClear}
              className="px-3 py-1.5 rounded-lg text-sm bg-red-900/50 hover:bg-red-900/70 text-red-300 transition-colors"
            >
              Clear
            </button>
          </div>
        </div>

        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="flex-1 min-h-0 overflow-y-auto rounded-lg border border-gray-700 bg-gray-900/80 font-mono text-xs"
        >
          {loading ? (
            <p className="text-gray-500 text-center py-12">Loading logs…</p>
          ) : visible.length === 0 ? (
            <p className="text-gray-500 text-center py-12">
              {errorsOnly ? 'No errors logged.' : 'No log entries yet.'}
            </p>
          ) : (
            visible.map(entry => (
              <div
                key={entry.id}
                className="flex gap-3 px-3 py-1 border-b border-gray-800/60 hover:bg-gray-800/40"
              >
                <span className="text-gray-600 shrink-0">{formatTime(entry.timestamp)}</span>
                <span
                  className={`shrink-0 w-16 text-center rounded px-1 text-[10px] font-semibold leading-4 self-start mt-0.5 ${
                    LEVEL_BADGES[entry.level] || LEVEL_BADGES.INFO
                  }`}
                >
                  {entry.level}
                </span>
                <span className="text-gray-600 shrink-0 w-28 truncate" title={entry.source}>
                  {entry.source}
                </span>
                <span className={`whitespace-pre-wrap break-all ${LEVEL_TEXT[entry.level] || 'text-gray-300'}`}>
                  {entry.message}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
