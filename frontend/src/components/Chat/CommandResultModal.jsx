import { useEffect } from 'react';
import MarkdownRenderer from '../shared/MarkdownRenderer';

// Popup for slash-command output. Commands are ephemeral status readouts, so
// their result shows here instead of cluttering the story transcript.
export default function CommandResultModal({ result, onClose }) {
  useEffect(() => {
    if (!result) return;
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [result, onClose]);

  if (!result) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      role="dialog"
      aria-modal="true"
      onMouseDown={onClose}
    >
      <div
        className="bg-gray-800 w-full max-w-lg rounded-lg shadow-2xl border border-gray-700 flex flex-col max-h-[80vh]"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="p-4 border-b border-gray-700 flex justify-between items-center bg-gray-900 rounded-t-lg">
          <h2 className="text-lg font-bold text-gray-100 flex items-center gap-2 min-w-0">
            {result.icon && <span className="shrink-0">{result.icon}</span>}
            <span className="font-mono text-purple-300 truncate">{result.command}</span>
            {result.name && <span className="text-sm text-gray-500 truncate">· {result.name}</span>}
          </h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-white text-2xl leading-none shrink-0 ml-2"
            aria-label="Close"
          >
            &times;
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-4 text-gray-200">
          <MarkdownRenderer content={result.message || ''} />
        </div>
      </div>
    </div>
  );
}
