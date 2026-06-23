import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useLLMInspector } from '../../hooks/useLLMInspector';
import LLMCallCard from './LLMCallCard';

export default function LLMInspectorPanel() {
  const { calls, isOpen, expandedIds, toggleExpand, clearCalls, setIsOpen } = useLLMInspector();
  const panelRef = useRef(null);

  useEffect(() => {
    if (!isOpen) return;
    function handleClick(e) {
      if (panelRef.current && !panelRef.current.contains(e.target)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [isOpen, setIsOpen]);

  if (!isOpen) return null;

  const uniqueCalls = [];
  const seen = new Set();
  for (const c of calls) {
    if (!seen.has(c.id)) {
      seen.add(c.id);
      uniqueCalls.push(c);
    }
  }

  const panel = (
    <div className="fixed inset-0 z-[9998] pointer-events-none">
      <div
        ref={panelRef}
        className="pointer-events-auto absolute top-0 right-0 h-full w-96 bg-gray-950 border-l border-gray-800 shadow-2xl flex flex-col"
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800 shrink-0">
          <h2 className="text-sm font-semibold text-gray-200">
            LLM Call Inspector
            <span className="ml-2 text-xs text-gray-500">({uniqueCalls.length})</span>
          </h2>
          <div className="flex items-center gap-2">
            <button
              onClick={clearCalls}
              className="text-[10px] px-2 py-1 rounded bg-red-900/40 text-red-300 hover:bg-red-900/60 transition-colors"
              title="Clear all calls"
            >
              Clear
            </button>
            <button
              onClick={() => setIsOpen(false)}
              className="text-gray-500 hover:text-gray-300 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
