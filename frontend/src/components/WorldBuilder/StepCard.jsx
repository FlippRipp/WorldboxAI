import { useState, useEffect, useCallback } from 'react';
import { getStepDescriptor } from './steps/registry';

/**
 * StepCard — a generic, step-agnostic shell.
 *
 * It owns only the cross-cutting concerns shared by every step:
 *   - edit / editedData buffer
 *   - guidance note input
 *   - the approve / re-roll / edit action bar
 *
 * The step body and which actions are available are resolved from the step
 * UI registry (steps/registry.js) keyed by step id. There are no per-step
 * conditionals here, so adding or removing a step never touches this file.
 */
export default function StepCard({ step, state, onApprove, onReroll, onAddNote, onRerollItem, loading, onEnrich, onEnrichCommit, worldId, worldState }) {
  const [editing, setEditing] = useState(false);
  const [editedData, setEditedData] = useState(null);
  const [note, setNote] = useState('');
  const [enriching, setEnriching] = useState(false);
  const [rerollingKey, setRerollingKey] = useState(null);
  const approved = state?.approved || false;
  const stepNote = state?.note || '';

  const { component: Body, actions } = getStepDescriptor(step.id);

  useEffect(() => {
    if (state?.data) {
      setEditedData(JSON.parse(JSON.stringify(state.data)));
    }
  }, [state?.data]);

  const handleFieldChange = useCallback((key, val) => {
    setEditedData((prev) => ({ ...prev, [key]: val }));
  }, []);

  const handleRerollItem = useCallback(async (fieldKey, index) => {
    if (!onRerollItem) return;
    const items = editedData?.[fieldKey] || [];
    const key = `${fieldKey}:${index}`;
    setRerollingKey(key);
    try {
      const newItem = await onRerollItem(step.id, fieldKey, index, items);
      if (typeof newItem === 'string') {
        setEditing(true);
        setEditedData((prev) => {
          const next = [...(prev?.[fieldKey] || [])];
          next[index] = newItem;
          return { ...prev, [fieldKey]: next };
        });
      }
    } finally {
      setRerollingKey(null);
    }
  }, [onRerollItem, editedData, step.id]);

  const handleApprove = () => {
    onApprove(editing ? editedData : null);
    setEditing(false);
  };

  const fieldsDisabled = (!editing || loading);
  const headerColor = approved ? 'text-green-300' : 'text-purple-300';
  const borderColor = approved ? 'border-green-700/50' : 'border-gray-700';

  const bodyProps = {
    step,
    state,
    worldState,
    worldId,
    editedData,
    onFieldChange: handleFieldChange,
    onRerollItem: onRerollItem ? handleRerollItem : undefined,
    rerollingKey,
    disabled: fieldsDisabled,
    loading,
    enriching,
    onEnrichingChange: setEnriching,
    onEnrich,
    onEnrichCommit,
  };

  return (
    <div className={`p-5 bg-gray-800/80 border ${borderColor} rounded-lg space-y-4`}>
      <div className="flex items-center gap-2">
        {approved && <span className="text-green-400 text-sm">✓</span>}
        <h3 className={`text-xl font-semibold ${headerColor}`}>{step.label}</h3>
        {approved && <span className="text-xs text-green-500/70 ml-1">Approved</span>}
      </div>
      <p className="text-sm text-gray-400 -mt-3">{step.description}</p>

      {stepNote && (
        <div className="text-xs text-purple-400 italic bg-purple-900/20 border border-purple-800/30 rounded px-3 py-2">
          Note: {stepNote}
        </div>
      )}

      <Body {...bodyProps} />

      <div className="flex items-center gap-2 pt-2 border-t border-gray-700">
        {!approved && (
          <button
            onClick={handleApprove}
            disabled={loading || enriching}
            className="px-4 py-2 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors flex items-center gap-2"
          >
            {loading ? (
              <span className="inline-block w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
            ) : (
              <span>✓</span>
            )}
            Approve
          </button>
        )}

        {actions.reroll && (
          <button
            onClick={() => onReroll(editedData)}
            disabled={loading}
            className="px-4 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded-lg text-sm transition-colors"
          >
            🔄 Re-roll
          </button>
        )}

        {actions.edit && (
          <button
            onClick={() => setEditing(true)}
            disabled={loading}
            className={`px-4 py-2 rounded-lg text-sm transition-colors ${editing ? 'bg-yellow-700 hover:bg-yellow-600' : 'bg-gray-700 hover:bg-gray-600'} disabled:opacity-50`}
          >
            ✏️ {editing ? 'Editing...' : 'Edit'}
          </button>
        )}

        {actions.edit && editing && approved && (
          <button
            onClick={handleApprove}
            disabled={loading}
            className="px-4 py-2 bg-green-600 hover:bg-green-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors flex items-center gap-2"
          >
            {loading ? (
              <span className="inline-block w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
            ) : (
              <span>✓</span>
            )}
            Save Changes
          </button>
        )}

        <div className="flex-1" />

        <div className="flex items-center gap-2">
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Add guidance note..."
            disabled={loading}
            className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-300 focus:border-purple-500 focus:outline-none w-48"
          />
          <button
            onClick={() => {
              if (note.trim()) {
                onAddNote(note.trim());
                setNote('');
              }
            }}
            disabled={loading || !note.trim()}
            className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded-lg text-sm transition-colors"
          >
            📝 Note
          </button>
        </div>
      </div>
    </div>
  );
}
