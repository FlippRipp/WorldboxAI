import { useEffect, useRef } from 'react';

export default function STImportReport({ report, onAdd, onClose }) {
  const closeRef = useRef(null);
  useEffect(() => {
    const previousFocus = document.activeElement;
    closeRef.current?.focus();
    return () => previousFocus?.focus?.();
  }, []);
  const warnings = report.warnings || [];
  const stats = report.stats || {};
  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/75 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="st-import-title"
      onKeyDown={(event) => {
        if (event.key === 'Escape') { event.stopPropagation(); onClose(); }
        if (event.key === 'Tab') {
          const buttons = event.currentTarget.querySelectorAll('button:not(:disabled), a[href], summary');
          const first = buttons[0];
          const last = buttons[buttons.length - 1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        }
      }}>
      <div className="bg-gray-900 text-gray-100 w-full h-dvh sm:h-auto sm:max-h-[90dvh] sm:max-w-2xl sm:rounded-xl border border-gray-700 shadow-2xl flex flex-col min-h-0">
        <div className="shrink-0 p-4 border-b border-gray-700 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 id="st-import-title" className="font-semibold text-lg">SillyTavern import report</h3>
            <p className="text-sm text-gray-400 break-words mt-1">{report.fileName}</p>
          </div>
          <button ref={closeRef} onClick={onClose} className="min-h-11 min-w-11 rounded hover:bg-gray-800" aria-label="Close import report">✕</button>
        </div>
        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-4">
          <p className="text-sm">{stats.imported || 0} blocks {report.applied ? 'added to your draft' : 'ready to add'} · {stats.skipped || 0} skipped · {stats.total || 0} preset entries</p>
          <div className="rounded-lg bg-emerald-950/40 border border-emerald-800 p-3 text-sm text-emerald-100">
            Comments are cleaned. Supported variables and dice run when the prompt is built. Block order, enabled states, and supported action restrictions are preserved. Collapsible panels and supported dialogue colors render in chat.
          </div>
          {warnings.length > 0 ? <section aria-label="Compatibility notices" className="space-y-3">
            <h4 className="font-semibold text-amber-200">Review {warnings.length} compatibility notices</h4>
            <p className="text-sm text-gray-300">This is partial SillyTavern support. The items below explain what was skipped, changed, or still needs attention.</p>
            <ul className="space-y-2">
              {warnings.map((warning, index) => <li key={`${warning.code}-${warning.block_id || ''}-${index}`} className="p-3 rounded-lg bg-amber-950/30 border border-amber-800/60 text-sm break-words">
                {warning.block_name && <div className="font-semibold text-amber-100 mb-1">{warning.block_name}</div>}
                <div className="text-gray-200">{warning.message}</div>
                {warning.details && <details className="mt-2 text-gray-400">
                  <summary className="cursor-pointer min-h-8">Show details</summary>
                  <p className="mt-1">{warning.details}</p>
                </details>}
              </li>)}
            </ul>
          </section> : <p className="text-sm text-gray-300">No unsupported features were detected in this preset.</p>}
          {!!stats.skipped_ids?.length && <details className="text-sm border border-gray-700 rounded-lg p-3">
            <summary className="cursor-pointer min-h-8">Skipped entries ({stats.skipped_ids.length})</summary>
            <ul className="mt-2 space-y-2 text-gray-400">{stats.skipped_ids.map((item, index) => <li key={index} className="break-words">{item.name || item.id}: {item.reason}</li>)}</ul>
          </details>}
        </div>
        <div className="shrink-0 p-4 pb-[max(1rem,env(safe-area-inset-bottom))] border-t border-gray-700 flex gap-2 justify-end">
          <button onClick={onClose} className="min-h-11 px-4 rounded bg-gray-700 hover:bg-gray-600">{report.applied ? 'Close' : 'Cancel'}</button>
          {!report.applied && <button onClick={onAdd} disabled={!report.blocks?.length} className="min-h-11 px-4 rounded bg-purple-600 hover:bg-purple-500 disabled:opacity-40">Add {report.blocks?.length || 0} blocks</button>}
        </div>
      </div>
    </div>
  );
}
