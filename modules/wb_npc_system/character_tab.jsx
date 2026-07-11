import React, { useEffect, useMemo, useState } from 'react';

// Character browser tab for the NPC System, hosted in the storyteller's
// Character view. Lists every character in the bank; ones the player hasn't
// met yet are blurred behind a per-card spoiler reveal. Introduced characters
// can be refreshed from the recent story (/npc update) and every character can
// be edited manually (/npc edit). Characters can also be created from scratch
// (/npc add) or removed (/npc delete). Receives { state, config, onCommand, busy }.

const ROLES = ['quest_giver', 'antagonist', 'ally', 'informant', 'rival', 'neutral', 'wildcard'];
const STATUSES = ['active', 'departed', 'deceased', 'unintroduced'];

const ROLE_COLORS = {
  quest_giver: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  antagonist: 'bg-red-500/15 text-red-300 border-red-500/30',
  ally: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  informant: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  rival: 'bg-orange-500/15 text-orange-300 border-orange-500/30',
  neutral: 'bg-gray-500/15 text-gray-300 border-gray-500/30',
  wildcard: 'bg-purple-500/15 text-purple-300 border-purple-500/30',
};

function Badge({ className = '', children }) {
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded-full border whitespace-nowrap ${className}`}>
      {children}
    </span>
  );
}

function Field({ label, children }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1">{label}</div>
      {children}
    </div>
  );
}

const inputCls = 'w-full bg-gray-900/70 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-indigo-500';

function AddForm({ busy, onCreate, onCancel }) {
  const [draft, setDraft] = useState({
    name: '', archetype: '', race: '', gender: '', role: 'neutral',
    appearance: '', pitch: '', personality: '', notes: '', introduced: true,
  });

  const set = (key) => (e) => setDraft((d) => ({ ...d, [key]: e.target.value }));

  const create = () => {
    const name = draft.name.trim();
    if (!name) return;
    const payload = { name, introduced: draft.introduced };
    for (const key of ['archetype', 'race', 'gender', 'appearance', 'pitch', 'notes']) {
      const v = draft[key].trim();
      if (v) payload[key] = v;
    }
    if (draft.role) payload.role = draft.role;
    const traits = draft.personality.split(',').map((t) => t.trim()).filter(Boolean);
    if (traits.length) payload.personality = traits;
    onCreate(payload);
  };

  return (
    <div className="space-y-3 bg-gray-800/60 border border-indigo-500/30 rounded-lg p-3">
      <div className="text-xs font-semibold text-indigo-300">New character</div>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name"><input className={inputCls} value={draft.name} onChange={set('name')} placeholder="Required" /></Field>
        <Field label="Archetype"><input className={inputCls} value={draft.archetype} onChange={set('archetype')} /></Field>
        <Field label="Race"><input className={inputCls} value={draft.race} onChange={set('race')} /></Field>
        <Field label="Gender"><input className={inputCls} value={draft.gender} onChange={set('gender')} /></Field>
        <Field label="Role">
          <select className={inputCls} value={draft.role} onChange={set('role')}>
            {ROLES.map((r) => <option key={r} value={r}>{r.replace(/_/g, ' ')}</option>)}
          </select>
        </Field>
        <Field label="Standing">
          <select
            className={inputCls}
            value={draft.introduced ? 'known' : 'pending'}
            onChange={(e) => setDraft((d) => ({ ...d, introduced: e.target.value === 'known' }))}
          >
            <option value="known">Already met</option>
            <option value="pending">Unintroduced (in the wings)</option>
          </select>
        </Field>
      </div>
      <Field label="Appearance"><textarea className={inputCls} rows={2} value={draft.appearance} onChange={set('appearance')} /></Field>
      <Field label="Pitch"><textarea className={inputCls} rows={2} value={draft.pitch} onChange={set('pitch')} /></Field>
      <Field label="Personality (comma-separated)"><input className={inputCls} value={draft.personality} onChange={set('personality')} /></Field>
      <Field label="Notes"><textarea className={inputCls} rows={2} value={draft.notes} onChange={set('notes')} /></Field>
      <div className="flex gap-2 justify-end">
        <button
          onClick={onCancel}
          className="text-xs px-3 py-1.5 rounded-lg border border-gray-600 text-gray-400 hover:text-gray-200 transition-colors"
        >
          Cancel
        </button>
        <button
          onClick={create}
          disabled={busy || !draft.name.trim()}
          className="text-xs px-3 py-1.5 rounded-lg border border-indigo-500/30 bg-indigo-500/15 text-indigo-300 hover:bg-indigo-500/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Create
        </button>
      </div>
    </div>
  );
}

function EditForm({ npc, busy, onSave, onCancel }) {
  const [draft, setDraft] = useState({
    name: npc.name || '',
    race: npc.race || '',
    gender: npc.gender || '',
    archetype: npc.archetype || '',
    role: npc.role || 'neutral',
    status: npc.status || 'active',
    appearance: npc.appearance || '',
    pitch: npc.pitch || '',
    personality: (npc.personality || []).join(', '),
    notes: npc.notes || '',
  });

  const set = (key) => (e) => setDraft((d) => ({ ...d, [key]: e.target.value }));

  const save = () => {
    const changed = {};
    if (draft.name.trim() && draft.name.trim() !== (npc.name || '')) changed.name = draft.name.trim();
    if (draft.race.trim() !== (npc.race || '')) changed.race = draft.race.trim();
    if (draft.gender.trim() !== (npc.gender || '')) changed.gender = draft.gender.trim();
    if (draft.archetype.trim() !== (npc.archetype || '')) changed.archetype = draft.archetype.trim();
    if (draft.role !== npc.role) changed.role = draft.role;
    if (draft.status !== npc.status) changed.status = draft.status;
    if (draft.appearance.trim() !== (npc.appearance || '')) changed.appearance = draft.appearance.trim();
    if (draft.pitch.trim() !== (npc.pitch || '')) changed.pitch = draft.pitch.trim();
    if (draft.notes.trim() !== (npc.notes || '')) changed.notes = draft.notes.trim();
    const traits = draft.personality.split(',').map((t) => t.trim()).filter(Boolean);
    if (traits.join(', ') !== (npc.personality || []).join(', ')) changed.personality = traits;
    onSave(changed);
  };

  return (
    <div className="space-y-3 bg-gray-800/60 border border-gray-700 rounded-lg p-3 mt-2">
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name"><input className={inputCls} value={draft.name} onChange={set('name')} /></Field>
        <Field label="Archetype"><input className={inputCls} value={draft.archetype} onChange={set('archetype')} /></Field>
        <Field label="Race"><input className={inputCls} value={draft.race} onChange={set('race')} /></Field>
        <Field label="Gender"><input className={inputCls} value={draft.gender} onChange={set('gender')} /></Field>
        <Field label="Role">
          <select className={inputCls} value={draft.role} onChange={set('role')}>
            {ROLES.map((r) => <option key={r} value={r}>{r.replace(/_/g, ' ')}</option>)}
          </select>
        </Field>
        <Field label="Status">
          <select className={inputCls} value={draft.status} onChange={set('status')}>
            {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </Field>
      </div>
      <Field label="Appearance"><textarea className={inputCls} rows={3} value={draft.appearance} onChange={set('appearance')} /></Field>
      <Field label="Pitch"><textarea className={inputCls} rows={3} value={draft.pitch} onChange={set('pitch')} /></Field>
      <Field label="Personality (comma-separated)"><input className={inputCls} value={draft.personality} onChange={set('personality')} /></Field>
      <Field label="Notes"><textarea className={inputCls} rows={2} value={draft.notes} onChange={set('notes')} /></Field>
      <div className="flex gap-2 justify-end">
        <button
          onClick={onCancel}
          className="text-xs px-3 py-1.5 rounded-lg border border-gray-600 text-gray-400 hover:text-gray-200 transition-colors"
        >
          Cancel
        </button>
        <button
          onClick={save}
          disabled={busy}
          className="text-xs px-3 py-1.5 rounded-lg border border-indigo-500/30 bg-indigo-500/15 text-indigo-300 hover:bg-indigo-500/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Save
        </button>
      </div>
    </div>
  );
}

export default function CharacterTab({ state, onCommand, busy }) {
  const bank = state?.module_data?.wb_npc_system?.characters || {};
  const [query, setQuery] = useState('');
  const [revealed, setRevealed] = useState(() => new Set());
  const [expandedId, setExpandedId] = useState(null);
  const [editingId, setEditingId] = useState(null);
  const [updatingId, setUpdatingId] = useState(null);
  const [adding, setAdding] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState(null);
  const [genRequest, setGenRequest] = useState('');

  // The pending update is done once the turn stops being busy; the refreshed
  // record has already arrived via state_update by then.
  useEffect(() => {
    if (!busy) setUpdatingId(null);
  }, [busy]);

  const npcs = useMemo(() => {
    const all = Object.values(bank);
    const q = query.trim().toLowerCase();
    const filtered = q
      ? all.filter((n) => [n.name, n.archetype, n.role, n.race].some((v) => String(v || '').toLowerCase().includes(q)))
      : all;
    const known = filtered.filter((n) => n.introduced).sort((a, b) => (b.met_turn ?? 0) - (a.met_turn ?? 0));
    const hidden = filtered.filter((n) => !n.introduced).sort((a, b) => (b.created_turn ?? 0) - (a.created_turn ?? 0));
    return [...known, ...hidden];
  }, [bank, query]);

  const isSpoiler = (npc) => !npc.introduced && !revealed.has(npc.id);
  // A known character's relationships may point at someone the player hasn't
  // met yet — mask those names so the list itself doesn't spoil them.
  const relName = (rel) => {
    const target = bank[rel.npc_id];
    if (!target) return rel.npc_id;
    return target.introduced || revealed.has(target.id) ? target.name : '???';
  };

  const onCardClick = (npc) => {
    if (isSpoiler(npc)) {
      setRevealed((prev) => new Set(prev).add(npc.id));
      return;
    }
    setExpandedId((cur) => (cur === npc.id ? null : npc.id));
    setEditingId(null);
  };

  const requestUpdate = (npc) => {
    if (!onCommand || busy) return;
    setUpdatingId(npc.id);
    onCommand(`/npc update ${npc.id}`);
  };

  const saveEdits = (npc, changed) => {
    setEditingId(null);
    if (!onCommand || Object.keys(changed).length === 0) return;
    onCommand(`/npc edit ${npc.id} ${encodeURIComponent(JSON.stringify(changed))}`);
  };

  const createCharacter = (payload) => {
    setAdding(false);
    if (!onCommand) return;
    onCommand(`/npc add ${encodeURIComponent(JSON.stringify(payload))}`);
  };

  const generateCharacter = () => {
    if (!onCommand || busy) return;
    setAdding(false);
    const req = genRequest.trim();
    onCommand(req ? `/npc generate ${encodeURIComponent(req)}` : '/npc generate');
    setGenRequest('');
  };

  const deleteCharacter = (npc) => {
    setConfirmDeleteId(null);
    setExpandedId(null);
    if (!onCommand || busy) return;
    onCommand(`/npc delete ${npc.id}`);
  };

  const empty = Object.keys(bank).length === 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <input
          className="flex-1 bg-gray-800/60 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-indigo-500"
          placeholder="Search by name, archetype, role…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {onCommand && (
          <button
            onClick={() => setAdding((v) => !v)}
            className={`text-xs px-3 py-2 rounded-lg border whitespace-nowrap transition-colors ${
              adding
                ? 'border-gray-600 text-gray-400 hover:text-gray-200'
                : 'border-indigo-500/30 bg-indigo-500/15 text-indigo-300 hover:bg-indigo-500/25'
            }`}
          >
            {adding ? 'Close' : '+ Add'}
          </button>
        )}
      </div>

      {onCommand && (
        <div className="flex items-center gap-2">
          <input
            className="flex-1 bg-gray-800/60 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-purple-500 disabled:opacity-50"
            placeholder="Optional: describe the character to generate…"
            value={genRequest}
            disabled={busy}
            onChange={(e) => setGenRequest(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') generateCharacter(); }}
          />
          <button
            onClick={generateCharacter}
            disabled={busy}
            title="Generate a character (optionally matching your request) and keep them hidden until you meet them"
            className="text-xs px-3 py-2 rounded-lg border whitespace-nowrap transition-colors border-purple-500/30 bg-purple-500/15 text-purple-300 hover:bg-purple-500/25 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            ✨ Generate
          </button>
        </div>
      )}
      {onCommand && busy && (
        <p className="text-xs text-purple-300/80 animate-pulse">
          Working… a generated character will appear (hidden) when the AI finishes.
        </p>
      )}

      {adding && (
        <AddForm busy={busy} onCreate={createCharacter} onCancel={() => setAdding(false)} />
      )}

      {empty && !adding && (
        <p className="text-sm text-gray-500 italic">
          No characters yet. Add one with “+ Add”, or let the NPC System fill its cast as the story progresses.
        </p>
      )}

      {npcs.map((npc) => {
        const spoiler = isSpoiler(npc);
        const expanded = expandedId === npc.id && !spoiler;
        const blur = spoiler ? 'blur-sm select-none' : '';

        return (
          <div
            key={npc.id}
            className={`bg-gray-800/40 border border-gray-700/60 rounded-lg transition-colors ${spoiler ? 'cursor-pointer hover:border-amber-500/40' : ''}`}
          >
            <div
              className={`p-3 flex items-center gap-3 ${spoiler ? '' : 'cursor-pointer'}`}
              onClick={() => onCardClick(npc)}
              title={spoiler ? 'Click to reveal this character (spoiler!)' : undefined}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`text-sm font-semibold text-gray-100 ${blur}`}>{npc.name}</span>
                  <span className={`text-xs text-gray-500 capitalize ${blur}`}>
                    {[npc.race, npc.gender].filter(Boolean).join(' · ')}
                  </span>
                </div>
                <div className={`text-xs text-gray-400 truncate ${blur}`}>{npc.archetype}</div>
              </div>
              <div className="flex items-center gap-1.5 flex-wrap justify-end">
                {spoiler && <Badge className="bg-amber-500/15 text-amber-300 border-amber-500/30">Spoiler</Badge>}
                {npc.traveling_with_player && <Badge className="bg-indigo-500/15 text-indigo-300 border-indigo-500/30">Party</Badge>}
                <Badge className={`capitalize ${ROLE_COLORS[npc.role] || ROLE_COLORS.neutral} ${blur}`}>
                  {String(npc.role || 'neutral').replace(/_/g, ' ')}
                </Badge>
                {npc.introduced && npc.status !== 'active' && (
                  <Badge className="bg-gray-500/15 text-gray-400 border-gray-500/30 capitalize">{npc.status}</Badge>
                )}
              </div>
            </div>

            {expanded && (
              <div className="px-3 pb-3 space-y-3 border-t border-gray-700/50 pt-3">
                {npc.appearance && (
                  <Field label="Appearance"><p className="text-sm text-gray-300 leading-relaxed">{npc.appearance}</p></Field>
                )}
                {npc.pitch && (
                  <Field label="Pitch"><p className="text-sm text-gray-300 leading-relaxed">{npc.pitch}</p></Field>
                )}
                {(npc.personality || []).length > 0 && (
                  <Field label="Personality">
                    <div className="flex flex-wrap gap-1">
                      {npc.personality.map((t, i) => (
                        <span key={i} className="text-xs px-2 py-0.5 bg-gray-700/60 text-gray-300 rounded capitalize">{t}</span>
                      ))}
                    </div>
                  </Field>
                )}
                {npc.notes && (
                  <Field label="Notes"><p className="text-sm text-gray-300 leading-relaxed">{npc.notes}</p></Field>
                )}
                {(npc.relationships || []).length > 0 && (
                  <Field label="Relationships">
                    <div className="text-sm text-gray-300 space-y-0.5">
                      {npc.relationships.map((rel, i) => (
                        <div key={i}>
                          <span className="capitalize text-gray-400">{String(rel.type || '?').replace(/_/g, ' ')}</span>
                          {' of '}
                          <span className="font-medium">{relName(rel)}</span>
                          {rel.description ? <span className="text-gray-500"> — {rel.description}</span> : null}
                        </div>
                      ))}
                    </div>
                  </Field>
                )}
                <div className="text-xs text-gray-500">
                  {npc.introduced ? `Met on turn ${npc.met_turn ?? '?'}` : 'Not yet met'}
                  {' · '}created turn {npc.created_turn ?? '?'}
                  {npc.source === 'story' ? ' · captured from the story' : ''}
                </div>

                {(npc.change_log || []).length > 0 && (
                  <Field label="Change Log">
                    <div className="space-y-1">
                      {[...npc.change_log].reverse().map((entry, i) => (
                        <div key={i} className="flex items-start gap-2 text-xs">
                          <span className="font-mono text-indigo-300 bg-indigo-500/15 border border-indigo-500/30 rounded px-1 whitespace-nowrap">
                            T{entry.turn ?? '?'}
                          </span>
                          <span className="text-gray-400">{entry.note}</span>
                        </div>
                      ))}
                    </div>
                  </Field>
                )}

                {editingId === npc.id ? (
                  <EditForm
                    npc={npc}
                    busy={busy}
                    onSave={(changed) => saveEdits(npc, changed)}
                    onCancel={() => setEditingId(null)}
                  />
                ) : confirmDeleteId === npc.id ? (
                  <div className="flex flex-wrap items-center gap-2 pt-1">
                    <span className="text-xs text-gray-400">Delete {npc.name} for good?</span>
                    <button
                      onClick={() => deleteCharacter(npc)}
                      disabled={busy}
                      className="text-xs px-3 py-1.5 rounded-lg border border-red-500/40 bg-red-500/15 text-red-300 hover:bg-red-500/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      Confirm delete
                    </button>
                    <button
                      onClick={() => setConfirmDeleteId(null)}
                      className="text-xs px-3 py-1.5 rounded-lg border border-gray-600 text-gray-400 hover:text-gray-200 transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <div className="flex flex-wrap gap-2 pt-1">
                    {onCommand && (
                      <button
                        onClick={() => requestUpdate(npc)}
                        disabled={busy}
                        className="text-xs px-3 py-1.5 rounded-lg border border-indigo-500/30 bg-indigo-500/15 text-indigo-300 hover:bg-indigo-500/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        Update from Story
                      </button>
                    )}
                    <button
                      onClick={() => setEditingId(npc.id)}
                      className="text-xs px-3 py-1.5 rounded-lg border border-gray-600 text-gray-400 hover:text-gray-200 transition-colors"
                    >
                      Edit
                    </button>
                    {onCommand && (
                      <button
                        onClick={() => setConfirmDeleteId(npc.id)}
                        className="text-xs px-3 py-1.5 rounded-lg border border-red-500/30 text-red-300/80 hover:bg-red-500/15 hover:text-red-300 transition-colors ml-auto"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                )}
                {busy && updatingId === npc.id && (
                  <p className="text-xs text-indigo-300/80 animate-pulse">
                    Checking the recent story for changes… the record will refresh when the AI finishes.
                  </p>
                )}
              </div>
            )}
          </div>
        );
      })}

      {!empty && npcs.length === 0 && (
        <p className="text-sm text-gray-500 italic">No characters match your search.</p>
      )}
    </div>
  );
}
