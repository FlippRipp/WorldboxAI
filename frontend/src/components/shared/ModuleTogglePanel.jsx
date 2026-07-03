import { useState, useRef, useEffect } from 'react';

// A compact button that opens a dropdown listing all modules with on/off
// toggles. Used by both the storyteller start screen and the character creator
// to choose which modules contribute UI. The host owns the enabled set and any
// per-module form data; this component only renders the toggles. Hosts should
// keep a disabled module's entered data in state (not delete it) so re-enabling
// restores it — persistence to disk happens at save time.
export default function ModuleTogglePanel({ modules = [], enabled, onToggle, label = 'Modules' }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onClick = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, [open]);

  const isEnabled = (id) => (enabled instanceof Set ? enabled.has(id) : (enabled || []).includes(id));
  const enabledCount = modules.filter((m) => isEnabled(m.id)).length;

  return (
    <div className="relative inline-block" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-gray-700 bg-gray-800 hover:bg-gray-700 text-sm text-gray-300 transition-colors"
        title="Toggle which modules are active"
      >
        <span>🧩</span>
        <span>{label}</span>
        <span className="text-xs text-gray-500">({enabledCount}/{modules.length})</span>
      </button>

      {open && (
        <div className="absolute z-30 mt-2 w-64 right-0 rounded-lg border border-gray-700 bg-gray-900 shadow-xl p-2">
          {modules.length === 0 ? (
            <p className="text-xs text-gray-500 p-2">No modules loaded.</p>
          ) : (
            modules.map((m) => {
              const on = isEnabled(m.id);
              return (
                <button
                  key={m.id}
                  onClick={() => onToggle(m.id, !on)}
                  className="w-full flex items-center justify-between px-2 py-2 rounded hover:bg-gray-800 transition-colors text-left"
                >
                  <span className="text-sm text-gray-200 truncate">{m.name || m.id}</span>
                  <span
                    className={`shrink-0 w-9 h-5 rounded-full relative transition-colors ${on ? 'bg-purple-600' : 'bg-gray-600'}`}
                  >
                    <span
                      className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all ${on ? 'left-[1.125rem]' : 'left-0.5'}`}
                    />
                  </span>
                </button>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
