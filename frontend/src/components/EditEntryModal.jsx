import { useState } from 'react';

// Generic field-driven edit dialog shared by the Memory browser and the
// Lorebook manager. `fields` drives the form ({key, label, type, rows?, min?,
// max?}); onSave receives (values, changed) where `changed` holds only the
// fields the user actually touched, so callers can send minimal patches and
// the backend re-embeds only when the embedded text itself changed.
export default function EditEntryModal({ title, fields, initialValues, onSave, onCancel, onDone }) {
  const [values, setValues] = useState(initialValues);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const setValue = (key, value) => setValues(prev => ({ ...prev, [key]: value }));

  const handleSave = async () => {
    setBusy(true);
    setError('');
    const changed = {};
    for (const f of fields) {
      if (values[f.key] !== initialValues[f.key]) changed[f.key] = values[f.key];
    }
    try {
      await onSave(values, changed);
      onDone();
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div className="bg-gray-800 border border-gray-600 rounded-lg p-5 shadow-xl w-full max-w-lg max-h-[80vh] overflow-y-auto">
        <h3 className="text-gray-100 font-semibold mb-4">{title}</h3>
        <div className="space-y-3">
          {fields.map(f => (
            <div key={f.key}>
              {f.type === 'checkbox' ? (
                <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={!!values[f.key]}
                    onChange={e => setValue(f.key, e.target.checked)}
                    className="accent-purple-600"
                    disabled={busy}
                  />
                  {f.label}
                </label>
              ) : (
                <>
                  <label className="block text-xs text-gray-400 mb-1">{f.label}</label>
                  {f.type === 'textarea' ? (
                    <textarea
                      value={values[f.key]}
                      onChange={e => setValue(f.key, e.target.value)}
                      rows={f.rows || 4}
                      className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200"
                      disabled={busy}
                    />
                  ) : (
                    <input
                      type={f.type === 'number' ? 'number' : 'text'}
                      value={values[f.key]}
                      min={f.min}
                      max={f.max}
                      onChange={e => setValue(f.key, f.type === 'number' ? Number(e.target.value) : e.target.value)}
                      className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200"
                      disabled={busy}
                    />
                  )}
                </>
              )}
            </div>
          ))}
        </div>
        {error && <p className="text-xs text-red-400 mt-3">{error}</p>}
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onCancel} disabled={busy} className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors">
            Cancel
          </button>
          <button onClick={handleSave} disabled={busy} className="px-3 py-1.5 bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-white rounded text-sm transition-colors">
            {busy ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}

// Fields for editing a shared lorebook entry (used by both the story tab and
// the Lorebook manager). Sticky is a text field so blank can mean "inherit
// the book default"; parse edits with parseStickyPatch before sending.
export const LOREBOOK_ENTRY_FIELDS = [
  { key: 'title', label: 'Title', type: 'text' },
  { key: 'keys', label: 'Keywords (comma-separated)', type: 'text' },
  { key: 'content', label: 'Content', type: 'textarea', rows: 6 },
  { key: 'enabled', label: 'Enabled', type: 'checkbox' },
  { key: 'constant', label: 'Constant (always in context)', type: 'checkbox' },
  { key: 'sticky_turns', label: 'Sticky turns (blank = use the book default)', type: 'text' },
  { key: 'injection_depth', label: 'Injection depth (blank = normal lore block; 0 = chat bottom, N = N messages up)', type: 'text' },
];

export function lorebookEntryInitialValues(entry) {
  return {
    title: entry.title || '',
    keys: (entry.keys || []).join(', '),
    content: entry.content || '',
    enabled: !!entry.enabled,
    constant: !!entry.constant,
    sticky_turns: entry.sticky_turns ?? '',
    injection_depth: entry.injection_depth ?? '',
  };
}

// Turn the modal's raw changed-values into an API patch: split keyword
// strings and map blank numeric fields to null (clear the setting).
export function lorebookEntryPatch(changed) {
  const patch = { ...changed };
  if ('keys' in patch) patch.keys = patch.keys.split(',').map(k => k.trim()).filter(Boolean);
  for (const key of ['sticky_turns', 'injection_depth']) {
    if (key in patch) {
      const raw = String(patch[key]).trim();
      patch[key] = raw === '' ? null : Math.max(0, parseInt(raw, 10) || 0);
    }
  }
  return patch;
}
