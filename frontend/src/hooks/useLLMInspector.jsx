import { createContext, useContext, useState, useCallback, useMemo, useRef, useEffect } from 'react';
import { api } from '../lib/api';

// Split into data (calls, open state — changes often) and actions (stable
// callbacks). App-level consumers that only dispatch (e.g. AppContent wiring
// addCall into the websocket) subscribe to actions only, so a new LLM call
// arriving doesn't re-render the whole app.
const LLMInspectorContext = createContext(null);
const LLMInspectorActionsContext = createContext(null);

export function LLMInspectorProvider({ children }) {
  const [calls, setCalls] = useState([]);
  const [isOpen, setIsOpen] = useState(false);
  const [expandedIds, setExpandedIds] = useState(new Set());
  const pollRef = useRef(null);
  const lastIdRef = useRef('');

  const togglePanel = useCallback(() => setIsOpen(v => !v), []);

  const toggleExpand = useCallback((id) => {
    setExpandedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const addCall = useCallback((call) => {
    setCalls(prev => {
      // Upsert: a call is broadcast once when it starts (status "running") and
      // again when it finishes, both with the same id. Merge into the existing
      // entry so the running entry fills out in place instead of duplicating.
      const idx = prev.findIndex(c => c.id === call.id);
      if (idx !== -1) {
        const next = prev.slice();
        next[idx] = { ...next[idx], ...call };
        return next;
      }
      if (call.id) lastIdRef.current = call.id;
      return [call, ...prev];
    });
  }, []);

  const clearCalls = useCallback(async () => {
    try { await api.clearLLMInspectorCalls(); } catch (_) {}
    setCalls([]);
    setExpandedIds(new Set());
  }, []);

  const poll = useCallback(async () => {
    try {
      const data = await api.getLLMInspectorCalls(lastIdRef.current || '', 50);
      const newCalls = data.calls || [];
      if (newCalls.length > 0) {
        setCalls(prev => {
          // Upsert returned calls (their status/output may have changed since
          // we last saw them) without disturbing entries we already hold. Keep
          // the previous array identity when nothing actually changed, so a
          // redundant poll response never re-renders consumers.
          const byId = new Map(prev.map(c => [c.id, c]));
          const merged = [...prev];
          let changed = false;
          for (const c of newCalls) {
            const idx = merged.findIndex(m => m.id === c.id);
            if (idx !== -1) {
              const cur = merged[idx];
              if (Object.keys(c).some(k => cur[k] !== c[k])) {
                merged[idx] = { ...cur, ...c };
                changed = true;
              }
            } else if (!byId.has(c.id)) {
              merged.unshift(c);
              changed = true;
            }
          }
          return changed ? merged : prev;
        });
        lastIdRef.current = newCalls[0].id;
      }
    } catch (_) {}
  }, []);

  // Initial load
  useEffect(() => {
    (async () => {
      try {
        const data = await api.getLLMInspectorCalls('', 50);
        if (data.calls?.length > 0) {
          setCalls(data.calls);
          lastIdRef.current = data.calls[0].id;
        }
      } catch (_) {}
    })();
  }, []);

  // Poll every 3s
  useEffect(() => {
    pollRef.current = setInterval(poll, 3000);
    return () => clearInterval(pollRef.current);
  }, [poll]);

  const actions = useMemo(
    () => ({ addCall, togglePanel, toggleExpand, clearCalls, setIsOpen }),
    [addCall, togglePanel, toggleExpand, clearCalls]
  );

  return (
    <LLMInspectorActionsContext.Provider value={actions}>
      <LLMInspectorContext.Provider value={{ calls, isOpen, expandedIds, addCall, togglePanel, toggleExpand, clearCalls, setIsOpen }}>
        {children}
      </LLMInspectorContext.Provider>
    </LLMInspectorActionsContext.Provider>
  );
}

export function useLLMInspector() {
  const ctx = useContext(LLMInspectorContext);
  if (!ctx) throw new Error('useLLMInspector must be inside LLMInspectorProvider');
  return ctx;
}

// Stable action callbacks only — subscribing to this never re-renders on new
// calls or panel toggles.
export function useLLMInspectorActions() {
  const ctx = useContext(LLMInspectorActionsContext);
  if (!ctx) throw new Error('useLLMInspectorActions must be inside LLMInspectorProvider');
  return ctx;
}
