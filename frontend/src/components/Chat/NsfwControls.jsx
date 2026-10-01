import { useState } from 'react';

function Summary({ section, busy, onSave }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  return (
    <details className="text-sm border-t border-gray-700 py-2">
      <summary className="cursor-pointer text-gray-300">AI summary · turns {section.start_turn}–{section.end_turn ?? section.start_turn}</summary>
      {editing ? <>
        <textarea aria-label="Edit AI summary" className="w-full mt-2 p-3 bg-gray-900 border border-gray-600 rounded" rows={6} value={draft} onChange={e => setDraft(e.target.value)} />
        <button disabled={busy || !draft.trim()} className="mr-3 text-purple-300 disabled:opacity-50" onClick={() => { onSave(section.id, draft); setEditing(false); }}>Save summary</button>
        <button onClick={() => setEditing(false)}>Cancel</button>
      </> : <>
        <p className="whitespace-pre-wrap my-2 text-gray-300">{section.summary}</p>
        <button disabled={busy} className="text-purple-300 disabled:opacity-50" onClick={() => { setDraft(section.summary); setEditing(true); }}>Edit summary</button>
      </>}
    </details>
  );
}

export default function NsfwControls({ value, busy, disabled, status, onAction }) {
  const data = value || {};
  const blocked = busy || disabled;
  const sections = (data.sections || []).filter(s => s.closed && s.summary);
  return (
    <div className="bg-gray-800 border-t border-gray-700 px-4 pt-3">
      <div className="max-w-[720px] mx-auto">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-2">
          <button role="switch" aria-checked={!!data.enabled} disabled={blocked}
            onClick={() => onAction('nsfw_mode', { enabled: !data.enabled })}
            className={`px-3 py-1.5 text-sm rounded-lg border disabled:opacity-50 ${data.enabled ? 'bg-amber-950 border-amber-600 text-amber-200' : 'border-gray-600 text-gray-300'}`}>
            NSFW mode: {data.enabled ? 'On' : 'Off'}
          </button>
          {data.failed_input != null && <button disabled={blocked} className="text-sm text-amber-200 disabled:opacity-50" onClick={() => onAction('nsfw_retry')}>Retry failed turn in NSFW mode</button>}
          {status?.stage === 'nsfw_summary' && <span role="status" className="text-sm text-purple-300">Preparing story summary…</span>}
        </div>
        {(sections.length > 0 || data.previous_attempts?.length > 0) && <details className="pb-2 text-sm text-gray-400">
          <summary className="cursor-pointer">AI summaries and previous attempts</summary>
          <div className="max-h-72 overflow-y-auto mt-2">
            {sections.map(section => <Summary key={section.id} section={section} busy={blocked} onSave={(section_id, summary) => onAction('nsfw_summary', { section_id, summary })} />)}
            {(data.previous_attempts || []).map((attempt, i) => <details key={i} className="py-2"><summary className="cursor-pointer">Previous attempt · turn {attempt.turn}</summary><p className="whitespace-pre-wrap mt-2">{attempt.content}</p></details>)}
          </div>
        </details>}
      </div>
    </div>
  );
}
