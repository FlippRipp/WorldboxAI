import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useLLMInspector } from '../../hooks/useLLMInspector';
import { useBackTrap } from '../../hooks/useBackNavigation';
import LLMCallCard from './LLMCallCard';

export default function LLMInspectorPanel() {
  const { calls, isOpen, expandedIds, toggleExpand, clearCalls, setIsOpen } = useLLMInspector();
  const panelRef = useRef(null);

  useBackTrap(isOpen, () => setIsOpen(false));

  useEffect(() => {
    if (!isOpen) return;
    function handleOutside(e) {
      if (e.target?.closest?.('[data-llm-inspector-btn]')) return;
      if (panelRef.current && !panelRef.current.contains(e.target)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('pointerdown', handleOutside);
    return () => document.removeEventListener('pointerdown', handleOutside);
  }, [isOpen, setIsOpen]);

  if (!isOpen) return null;

  const uniqueCalls = [];
  const seen = new Set();
  let sessionCostUsd = 0;
  for (const c of calls) {
    if (!seen.has(c.id)) {
      seen.add(c.id);
      uniqueCalls.push(c);
      if (typeof c.estimated_cost_usd === 'number' && c.estimated_cost_usd > 0) {
        sessionCostUsd += c.estimated_cost_usd;
      }
    }
  }

  const panel = (
    <div className="fixed inset-0 z-[9998]">
      {/* Dim backdrop — click to close */}
      <div
        className="absolute inset-0 bg-black/50"
        onClick={() => setIsOpen(false)}
      />
      <div
        ref={panelRef}
        className="absolute top-0 right-0 h-dvh w-full sm:w-1/2 sm:min-w-[480px] bg-gray-950 border-l border-gray-800 shadow-2xl flex flex-col"
      >
        <div className="flex items-center justify-between px-4 py-2 border-b border-gray-800 shrink-0">
          <h2 className="text-sm font-semibold text-gray-200 flex items-center gap-2 min-w-0">
            <span className="truncate">LLM Call Inspector</span>
            <span className="text-xs text-gray-500">({uniqueCalls.length})</span>
            {sessionCostUsd > 0 && (
              <span className="text-xs font-mono text-emerald-400 bg-emerald-950/50 border border-emerald-800/50 px-1.5 py-0.5 rounded">
                ${sessionCostUsd >= 0.01 ? sessionCostUsd.toFixed(4) : sessionCostUsd.toFixed(6)}
              </span>
            )}
          </h2>
          <div className="flex items-center gap-1 shrink-0">
            <button
              onClick={clearCalls}
              className="min-h-9 text-xs px-2.5 py-1 rounded-lg bg-red-900/40 text-red-300 hover:bg-red-900/60 transition-colors"
              title="Clear all calls"
            >
              Clear
            </button>
            <button
              onClick={() => setIsOpen(false)}
              className="min-h-11 min-w-11 -mr-2 flex items-center justify-center rounded-lg text-gray-400 hover:text-gray-200 hover:bg-gray-800 transition-colors"
              aria-label="Close LLM inspector"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-1.5">
          {uniqueCalls.length === 0 ? (
            <div className="text-center text-gray-600 text-xs py-12">
              No LLM calls recorded yet.
              <br />
              <span className="text-gray-700">Calls appear here as the engine generates content.</span>
            </div>
          ) : (
            uniqueCalls.map(call => (
              <LLMCallCard
                key={call.id}
                call={call}
                expanded={expandedIds.has(call.id)}
                onToggle={() => toggleExpand(call.id)}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );

  return createPortal(panel, document.body);
}
