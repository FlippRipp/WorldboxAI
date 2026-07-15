import { useState, useRef, useEffect } from 'react';

// Naming step for story branches: every branch action goes through this
// dialog before the fork is created. The input is prefilled with the
// auto-generated "<save> (branch @ turn N)" name, so a bare Enter keeps the
// old behavior. In-app modal (same styling as the settings dialogs), not a
// browser prompt.
export default function BranchNameDialog({ defaultName, busy, error, onConfirm, onCancel }) {
  const [name, setName] = useState(defaultName);
  const inputRef = useRef(null);
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const clean = name.trim();
  const confirm = () => {
    if (clean && !busy) onConfirm(clean);
  };

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Name the new branch"
      onClick={() => !busy && onCancel()}
      onKeyDown={(e) => { if (e.key === 'Escape' && !busy) onCancel(); }}
    >
      <div
        className="bg-gray-800 w-full max-w-md rounded-lg shadow-2xl border border-gray-700"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-4 border-b border-gray-700 bg-gray-900 rounded-t-lg">
          <h3 className="text-lg font-bold text-purple-400">Name the new branch</h3>
        </div>
        <div className="p-4">
          <p className="text-sm text-gray-400 mb-3">
            The story forks into a new save. Give the branch a name so you can tell it apart later.
          </p>
          <input
            ref={inputRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') confirm(); }}
            maxLength={120}
            disabled={busy}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-purple-500 disabled:opacity-50"
            aria-label="Branch name"
          />
          {error && <p className="text-sm text-red-400 mt-3">{error}</p>}
        </div>
        <div className="p-4 border-t border-gray-700 bg-gray-900 rounded-b-lg flex justify-end gap-3">
          <button
            onClick={onCancel}
            disabled={busy}
            className="px-4 py-2 text-sm text-gray-300 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded font-medium transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={confirm}
            disabled={busy || !clean}
            className="px-4 py-2 text-sm text-white bg-purple-700 hover:bg-purple-600 disabled:opacity-50 rounded font-medium transition-colors"
          >
            {busy ? 'Branching…' : 'Create Branch'}
          </button>
        </div>
      </div>
    </div>
  );
}
