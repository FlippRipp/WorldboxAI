import { useState, useMemo } from 'react';
import { api } from 'api';
import SchemaForm from '../WorldBuilder/steps/SchemaForm';
import TerrainStepView from '../WorldBuilder/steps/TerrainStepView';
import EnrichmentPanel from '../WorldBuilder/EnrichmentPanel';
import { joinKey } from './WorldExplorerScreen';

// Steps the map view / enrichment entry already represent — no entry here.
const HIDDEN_STEPS = new Set(['map_generation', 'node_labeling', 'node_descriptions']);
// Steps whose regeneration is safe on a saved world (the backend refuses the
// structural ones regardless; hierarchy_design is shown read-only because
// re-running it would desync the design record from the already-built maps).
const REGEN_STEPS = new Set(['world_form', 'world_rules', 'lore',
  'society_factions', 'natural_landmarks', 'layer_rules']);

const EXPANDABLE_TYPES = new Set(['city', 'settlement', 'port', 'stronghold']);

function Entry({ label, hint, badge, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-b border-gray-800">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2.5 text-left hover:bg-gray-800/40 transition-colors"
      >
        <div className="min-w-0">
          <div className="text-sm font-semibold text-gray-200">{label}</div>
          {hint && <div className="text-[11px] text-gray-500 truncate">{hint}</div>}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {badge}
          <span className="text-gray-600 text-xs">{open ? '−' : '+'}</span>
        </div>
      </button>
      {open && <div className="px-3 pb-3">{children}</div>}
    </div>
  );
}

function StepEntry({ worldId, step, entry, regenerable, onChanged }) {
  const [editing, setEditing] = useState(false);
  const [buffer, setBuffer] = useState(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const startEdit = () => {
    setBuffer(JSON.parse(JSON.stringify(entry.data)));
    setEditing(true);
  };

  const save = async () => {
    setBusy(true);
    setError('');
    try {
      await api.saveWorldStep(worldId, step.id, { ...entry, data: buffer });
      setEditing(false);
      await onChanged();
    } catch (e) {
      setError(e.message || 'Save failed.');
    } finally {
      setBusy(false);
    }
  };

  const regenerate = async () => {
    setBusy(true);
    setError('');
    try {
      await api.regenerateWorldStep(worldId, step.id, note.trim());
      setNote('');
      setEditing(false);
      await onChanged();
    } catch (e) {
      setError(e.message || 'Regeneration failed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <SchemaForm
        step={step}
        editedData={editing ? buffer : entry.data}
        onFieldChange={(key, val) => setBuffer((prev) => ({ ...prev, [key]: val }))}
        disabled={!editing || busy}
      />
      {error && <p className="text-xs text-red-400">{error}</p>}
      <div className="flex flex-wrap items-center gap-2 pt-1 border-t border-gray-800">
        {editing ? (
          <>
            <button
              onClick={save}
              disabled={busy}
              className="px-3 py-1.5 bg-green-700 hover:bg-green-600 disabled:opacity-50 rounded text-xs font-medium transition-colors"
            >
              {busy ? 'Saving…' : '✓ Save'}
            </button>
            <button
              onClick={() => setEditing(false)}
              disabled={busy}
              className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded text-xs transition-colors"
            >
              Cancel
            </button>
          </>
        ) : (
          <button
            onClick={startEdit}
            disabled={busy}
            className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded text-xs transition-colors"
          >
            ✏️ Edit
          </button>
        )}
        {regenerable && (
          <div className="flex items-center gap-1.5 flex-1 min-w-[180px]">
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Steering note (optional)…"
              disabled={busy}
              className="flex-1 min-w-0 bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-gray-300 focus:border-purple-500 focus:outline-none"
            />
            <button
              onClick={regenerate}
              disabled={busy}
              title="Regenerate this entry with the note as steering"
              className="px-3 py-1.5 bg-purple-700 hover:bg-purple-600 disabled:opacity-50 rounded text-xs transition-colors shrink-0"
            >
              {busy ? '…' : '🔄 Regenerate'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function BriefEntry({ brief, mapsById }) {
  const notes = brief.notes || [];
  const worldNotes = notes.filter((n) => !joinKey(n.subject));
  const subjectNotes = notes.filter((n) => joinKey(n.subject));
  const allKeys = useMemo(() => {
    const keys = new Set();
    Object.values(mapsById || {}).forEach((m) => {
      keys.add(joinKey(m.label));
      keys.add(joinKey(m.map_id));
      (m.nodes || []).forEach((n) => n.name && keys.add(joinKey(n.name)));
    });
    keys.delete('');
    return [...keys];
  }, [mapsById]);
  const isBound = (subject) => {
    const s = joinKey(subject);
    return allKeys.some((k) => k === s || k.includes(s) || s.includes(k));
  };

  return (
    <div className="space-y-3 text-xs">
      {brief.prompt && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Seed prompt</div>
          <p className="text-gray-300 leading-relaxed whitespace-pre-wrap">{brief.prompt}</p>
        </div>
      )}
      {(brief.rules || []).length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">World rules</div>
          <ul className="space-y-1">
            {brief.rules.map((r, i) => (
              <li key={i} className="text-gray-300 leading-relaxed">• {r}</li>
            ))}
          </ul>
        </div>
      )}
      {worldNotes.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Design notes (whole world)</div>
          <ul className="space-y-1">
            {worldNotes.map((n) => (
              <li key={n.id || n.text} className="text-gray-300 leading-relaxed">✎ {n.text}</li>
            ))}
          </ul>
        </div>
      )}
      {subjectNotes.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Design notes (specific places)</div>
          <ul className="space-y-1">
            {subjectNotes.map((n) => (
              <li key={n.id || n.text} className="text-gray-300 leading-relaxed">
                ✎ {n.text}
                <span className="text-gray-500"> — {n.subject}</span>
                {n.status === 'amended' && (
                  <span className="ml-1 text-[10px] text-amber-400 border border-amber-700/60 rounded px-1">amended</span>
                )}
                {!isBound(n.subject) && (
                  <span
                    className="ml-1 text-[10px] text-amber-400 border border-amber-700/60 rounded px-1"
                    title="Nothing on the maps matches this subject yet"
                  >
                    unbound
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {!brief.prompt && !(brief.rules || []).length && !notes.length && (
        <p className="text-gray-500 italic">This world has no recorded brief.</p>
      )}
    </div>
  );
}

function InteriorsEntry({ worldId, mapsById, siteMaps, onChanged }) {
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState('');

  const candidates = useMemo(() => {
    const anchored = new Set(
      Object.values(mapsById).map((m) => m.anchor_node_id).filter(Boolean));
    const out = [];
    Object.values(mapsById).forEach((m) => {
      (m.nodes || []).forEach((n) => {
        if (!n.name || !EXPANDABLE_TYPES.has(n.type) || (n.importance ?? 0) < 6) return;
        out.push({
          node: n,
          mapLabel: m.label || m.map_id,
          expanded: anchored.has(n.id) || !!siteMaps?.[n.id],
        });
      });
    });
    out.sort((a, b) => (b.node.importance ?? 0) - (a.node.importance ?? 0));
    return out;
  }, [mapsById, siteMaps]);

  if (!candidates.length) {
    return <p className="text-xs text-gray-500 italic">No major settlements to pre-expand.</p>;
  }

  const expand = async (nodeId) => {
    setBusyId(nodeId);
    setError('');
    try {
      await api.expandWorldSite(worldId, nodeId);
      await onChanged();
    } catch (e) {
      setError(e.message || 'Expansion failed.');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-2">
      <p className="text-[11px] text-gray-500">
        Pre-generate the inside of key locations. Anything left alone is
        generated the first time the story goes there.
      </p>
      {error && <p className="text-xs text-red-400">{error}</p>}
      <ul className="space-y-1.5">
        {candidates.map(({ node, mapLabel, expanded }) => (
          <li key={node.id} className="flex items-center justify-between gap-2 text-xs">
            <span className="text-gray-300 truncate">
              <span className="text-amber-400">{node.name}</span>
              <span className="text-gray-500"> ({node.type} · {mapLabel})</span>
            </span>
            {expanded ? (
              <span className="text-emerald-400 shrink-0">Expanded</span>
            ) : (
              <button
                onClick={() => expand(node.id)}
                disabled={busyId !== null}
                className="px-2 py-1 bg-purple-700 hover:bg-purple-600 disabled:opacity-50 rounded text-[11px] text-gray-100 transition-colors shrink-0"
              >
                {busyId === node.id ? 'Expanding…' : 'Expand'}
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * GlobalPanel — the world's global information entries: the ideation brief,
 * every info step's content (editable, regenerable-with-note where safe),
 * the terrain preview, the enrichment panel and interior pre-expansion.
 */
export default function GlobalPanel({ worldId, compiled, worldState, pipeline, mapsById, onChanged }) {
  const [enriching, setEnriching] = useState(false);
  const steps = worldState?.steps || {};
  const terrainEntry = steps.terrain_generation;

  const infoSteps = pipeline.filter((s) => !HIDDEN_STEPS.has(s.id)
    && s.id !== 'terrain_generation' && steps[s.id]?.data);

  return (
    <div>
      <div className="px-3 py-3 border-b border-gray-800">
        <h2 className="text-base font-bold text-gray-100">World Info</h2>
        <p className="text-[11px] text-gray-500 mt-0.5">
          Global entries — expand to read, edit or regenerate.
        </p>
      </div>

      {compiled?.brief && (
        <Entry label="Brief" hint="What this world was asked to be" defaultOpen>
          <BriefEntry brief={compiled.brief} mapsById={mapsById} />
        </Entry>
      )}

      {infoSteps.map((step) => (
        <Entry
          key={step.id}
          label={step.label}
          hint={step.description}
          badge={steps[step.id]?.note ? (
            <span className="text-[10px] text-purple-400" title={`Note: ${steps[step.id].note}`}>✎</span>
          ) : null}
        >
          <StepEntry
            worldId={worldId}
            step={step}
            entry={steps[step.id]}
            regenerable={REGEN_STEPS.has(step.id)}
            onChanged={onChanged}
          />
        </Entry>
      ))}

      {terrainEntry?.data?.layers?.length > 0 && (
        <Entry label="Terrain" hint="Generated surface rasters">
          <TerrainStepView editedData={terrainEntry.data} worldId={worldId} />
        </Entry>
      )}

      <Entry
        label="Enrichment"
        hint="Name and describe places with the AI"
        badge={enriching ? (
          <span className="inline-block w-3 h-3 border-2 border-purple-400/30 border-t-purple-400 rounded-full animate-spin" />
        ) : null}
      >
        <EnrichmentPanel
          stepId="explorer"
          stepLabel="Enrichment"
          data={compiled}
          worldId={worldId}
          enriching={enriching}
          onEnrichingChange={(v) => {
            setEnriching(v);
            // A finished run changed names/descriptions on disk — re-read
            // the compiled world so the map and lists show them.
            if (!v) onChanged();
          }}
        />
      </Entry>

      <Entry label="Interiors" hint="Pre-expand key locations">
        <InteriorsEntry
          worldId={worldId}
          mapsById={mapsById}
          siteMaps={compiled?.site_maps}
          onChanged={onChanged}
        />
      </Entry>
    </div>
  );
}
