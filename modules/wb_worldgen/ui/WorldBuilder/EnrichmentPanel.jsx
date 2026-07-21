import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { api } from 'api';
import { storage } from 'storage';
import { normalizeWorldData } from '../lib/mapspace';

function ProgressBar({ filled, total, label }) {
  const pct = total > 0 ? Math.round((filled / total) * 100) : 0;
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-gray-400">
        <span>{label}</span>
        <span>{filled}/{total}</span>
      </div>
      <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden">
        <div
          className="h-full bg-purple-500 rounded-full transition-all duration-300"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// The active run's intent, mirrored to localStorage so it survives Android
// killing the backgrounded PWA (which severs the SSE stream and cancels the
// run server-side; finished nodes are cached). On the next mount the run is
// restarted automatically and continues where it stopped — already-enriched
// nodes are skipped. Cleared whenever a run ends while the page is alive
// (done, stop, error, navigation), so only a hard kill triggers auto-resume.
const RUN_INTENT_KEY = 'wb_worldgen_enrich_run_intent';

// The panel is driven by the server's enrichment pass catalog
// (/enrich/passes): one selectable progress row per registered node pass, a
// run affordance per map pass, and per-pass progress merged live from the
// run's SSE events (which carry the pass id in their "phase" field). A pass
// module dropped into the backend appears here without frontend edits.
export default function EnrichmentPanel({ stepId, stepLabel, data, worldId, loading: parentLoading, enriching, onEnrichingChange, onResult }) {
  const [passes, setPasses] = useState([]);
  const [selectedPassId, setSelectedPassId] = useState(null);
  const [targetCount, setTargetCount] = useState(30);
  const [enrichResults, setEnrichResults] = useState([]);
  const [progress, setProgress] = useState(null); // /enrich/progress payload
  const [liveProgress, setLiveProgress] = useState({}); // passId -> SSE progress
  const [layerFilter, setLayerFilter] = useState('');
  const [sessionIds, setSessionIds] = useState({}); // passId -> node ids done this session
  const [reworkMode, setReworkMode] = useState(false);
  const abortRef = useRef(null); // in-flight run's AbortController
  const resumedRef = useRef(false);

  const nodePasses = useMemo(() => passes.filter((p) => p.unit === 'node'), [passes]);
  const mapPasses = useMemo(() => passes.filter((p) => p.unit !== 'node'), [passes]);

  useEffect(() => {
    if (!worldId) return;
    let alive = true;
    api.enrichPasses(worldId)
      .then((r) => { if (alive) setPasses(r.passes || []); })
      .catch((e) => console.error('Pass catalog fetch failed:', e));
    return () => { alive = false; };
  }, [worldId]);

  const fetchProgress = useCallback(async () => {
    if (!worldId) return;
    try {
      const p = await api.enrichProgress(worldId, layerFilter || null);
      setProgress(p);
      setLiveProgress({});
    } catch (e) {
      console.error('Progress fetch failed:', e);
    }
  }, [worldId, layerFilter]);

  useEffect(() => {
    fetchProgress();
  }, [fetchProgress]);

  useEffect(() => {
    return () => { abortRef.current?.abort(); };
  }, []);

  // Display numbers for one pass: live SSE state wins over the last fetch.
  const passProgress = useCallback((passId) => {
    const live = liveProgress[passId];
    if (live) {
      return { done: live.total_labeled || 0, total: live.total_nodes || 0, per_layer: live.per_layer || {} };
    }
    const server = progress?.passes?.[passId];
    if (server) {
      return { done: server.done || 0, total: server.total || 0, per_layer: server.per_layer || {} };
    }
    return null;
  }, [liveProgress, progress]);

  // Default selection: the first node pass with pending work, else the first.
  useEffect(() => {
    if (selectedPassId || !nodePasses.length || !progress) return;
    const firstPending = nodePasses.find((p) => {
      const pp = progress.passes?.[p.id];
      return pp && pp.done < pp.total;
    });
    setSelectedPassId((firstPending || nodePasses[0]).id);
  }, [nodePasses, progress, selectedPassId]);

  const selected = passProgress(selectedPassId);
  const selectedSpec = nodePasses.find((p) => p.id === selectedPassId) || null;
  const perLayer = selected?.per_layer || {};
  const totalNodes = selected?.total || 0;
  const totalDone = selected?.done || 0;
  const remaining = totalNodes - totalDone;
  const upfront = progress?.upfront || null;

  // Map/layer list for the filter dropdown and progress labels. `per_layer`
  // keys are map ids in world_format 2 (e.g. "root") and layer ids in legacy
  // data, so everything below iterates keys generically and only uses this
  // list for human-readable labels.
  const normalizedMaps = useMemo(() => normalizeWorldData(data), [data]);
  const mapList = useMemo(() => {
    const fromMaps = Object.values(normalizedMaps.mapsById)
      .map((m) => ({ id: m.map_id, label: m.label || m.map_id }));
    if (fromMaps.length) return fromMaps;
    // Legacy layer metadata without embedded maps.
    return (Array.isArray(data?.layers) ? data.layers : [])
      .map((l) => ({ id: l.layer_id, label: l.name || l.layer_id }));
  }, [normalizedMaps, data]);
  const labelFor = (id) => mapList.find((m) => m.id === id)?.label || id;
  const isComplete = !reworkMode && remaining <= 0 && totalNodes > 0;
  const reworkAvailable = totalDone > 0;
  const genCap = reworkMode ? (totalDone || 1) : (remaining || 1);

  const handleTargetChange = (e) => {
    setTargetCount(Math.max(1, parseInt(e.target.value) || 1));
  };

  // phaseArg is 'all', a pass id, or undefined (= the selected pass).
  // `overrides` lets the auto-resume path apply restored settings without
  // waiting a render for the state setters.
  const startEnriching = async (phaseArg, overrides = {}) => {
    const phase = phaseArg || selectedPassId;
    if (!worldId || !phase) return;
    const everything = phase === 'all';
    const isNodePhase = nodePasses.some((p) => p.id === phase);
    const count = overrides.count ?? targetCount;
    const layer = overrides.layerId !== undefined ? overrides.layerId : (layerFilter || null);
    const rework = overrides.rework ?? reworkMode;
    if (!everything && isNodePhase && (count <= 0 || (isComplete && !overrides.resumed))) return;
    const controller = new AbortController();
    abortRef.current = controller;
    onEnrichingChange(true);
    setEnrichResults([]);
    try {
      storage.setItem(RUN_INTENT_KEY, JSON.stringify({
        worldId, stepId, phase, count, layerId: layer, rework,
      }));
    } catch { /* private mode — resume just won't survive a kill */ }

    const newResults = [];
    const buckets = {}; // passId -> this run's per-pass session id arrays
    const bucketFor = (pid) => {
      if (!buckets[pid]) buckets[pid] = [...(sessionIds[pid] || [])];
      return buckets[pid];
    };

    const onEvent = (evt) => {
      if (evt.type === 'phase') {
        setLiveProgress((prev) => ({
          ...prev,
          [evt.phase]: {
            per_layer: evt.per_layer,
            total_nodes: evt.total_nodes,
            total_labeled: evt.total_labeled,
          },
        }));
        return;
      }
      if (evt.type === 'review_fix') {
        newResults.push({
          node_id: evt.node_id,
          label: evt.new,
          name: evt.new,
          description: null,
          layer_id: evt.map_id,
          review_fix: true,
          old: evt.old,
          problem: evt.problem,
        });
        setEnrichResults([...newResults]);
        onResult?.({ node_id: evt.node_id, label: evt.new });
        return;
      }
      if (evt.type !== 'node' && evt.type !== 'failed') return;

      if (evt.type === 'failed') {
        newResults.push({
          node_id: evt.node_id,
          label: null,
          name: null,
          description: null,
          layer_id: evt.layer_id,
          failed: true,
        });
      } else {
        const bucket = bucketFor(evt.phase);
        bucket.push(evt.node_id);
        setSessionIds((prev) => ({ ...prev, [evt.phase]: [...bucket] }));
        newResults.push({
          node_id: evt.node_id,
          label: evt.label,
          name: evt.label,
          description: evt.description,
          layer_id: evt.layer_id,
        });
        onResult?.({
          node_id: evt.node_id,
          label: evt.label,
          label_description: evt.label_description,
          description: evt.description,
          layer_id: evt.layer_id,
        });
      }
      setEnrichResults([...newResults]);
      if (evt.per_layer) {
        setLiveProgress((prev) => ({
          ...prev,
          [evt.phase]: {
            per_layer: evt.per_layer,
            total_nodes: evt.total_nodes,
            total_labeled: evt.total_labeled,
          },
        }));
      }
    };

    try {
      await api.enrichRun(
        worldId,
        {
          phase,
          count: everything || !isNodePhase ? null : count,
          layerId: layer,
          rework: !everything && isNodePhase && rework,
          excludeNodeIds: !everything && isNodePhase && rework
            ? bucketFor(phase)
            : null,
        },
        onEvent,
        controller.signal,
      );
    } catch (e) {
      if (e.name !== 'AbortError') console.error('Enrichment run failed:', e);
    } finally {
      try { storage.removeItem(RUN_INTENT_KEY); } catch { /* ignore */ }
      abortRef.current = null;
      onEnrichingChange(false);
      fetchProgress();
    }
  };

  const stopEnriching = () => {
    // Tell the server to stop (it flushes finished nodes), then drop the stream.
    try { storage.removeItem(RUN_INTENT_KEY); } catch { /* ignore */ }
    api.enrichCancel(worldId).catch(() => {});
    abortRef.current?.abort();
    abortRef.current = null;
    onEnrichingChange(false);
  };

  // Auto-resume a run that a process kill cut short (the intent key survives
  // only a hard kill — see RUN_INTENT_KEY). Rework runs are excluded: their
  // per-session exclude lists died with the process, so restarting one would
  // regenerate nodes the player already reworked.
  useEffect(() => {
    if (resumedRef.current || !worldId || enriching) return;
    let saved = null;
    try { saved = JSON.parse(storage.getItem(RUN_INTENT_KEY) || 'null'); } catch { /* ignore */ }
    if (!saved || saved.worldId !== worldId || saved.stepId !== stepId) return;
    resumedRef.current = true;
    if (saved.rework) {
      try { storage.removeItem(RUN_INTENT_KEY); } catch { /* ignore */ }
      return;
    }
    const phase = saved.phase || 'all';
    if (phase !== 'all') {
      if (saved.count) setTargetCount(saved.count);
      if (saved.layerId) setLayerFilter(saved.layerId);
      setSelectedPassId(phase);
    }
    startEnriching(phase, {
      count: saved.count, layerId: saved.layerId ?? null, rework: false, resumed: true,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [worldId, stepId, enriching]);

  const selectPass = (pid) => {
    setSelectedPassId(pid);
    setEnrichResults([]);
  };

  return (
    <div className="space-y-4 pt-2">
      <div className="bg-gray-800/60 border border-gray-700 rounded-lg p-4 space-y-3">
        {passes.length === 0 && (
          <span className="text-sm text-gray-400">Loading passes...</span>
        )}

        {nodePasses.map((p) => {
          const pp = passProgress(p.id);
          const isSelected = p.id === selectedPassId;
          const pct = pp && pp.total > 0 ? Math.round((pp.done / pp.total) * 100) : 0;
          return (
            <button
              key={p.id}
              onClick={() => selectPass(p.id)}
              disabled={enriching || parentLoading}
              title={p.description}
              className={`w-full text-left rounded-lg border px-3 py-2 space-y-1 transition-colors ${
                isSelected ? 'border-purple-500 bg-purple-500/10' : 'border-gray-700 hover:border-gray-500'
              }`}
            >
              <div className="flex justify-between text-xs">
                <span className={isSelected ? 'text-purple-300 font-medium' : 'text-gray-300'}>
                  {p.label}
                  {pp && pp.total > 0 && pp.done >= pp.total ? ' - Complete' : ''}
                </span>
                <span className="text-gray-400">{pp ? `${pp.done}/${pp.total}` : '...'}</span>
              </div>
              <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden">
                <div
                  className="h-full bg-purple-500 rounded-full transition-all duration-300"
                  style={{ width: `${pct}%` }}
                />
              </div>
            </button>
          );
        })}

        {mapPasses.map((p) => (
          <div
            key={p.id}
            className="flex items-center justify-between gap-3 rounded-lg border border-gray-700 px-3 py-2"
          >
            <div className="min-w-0">
              <div className="text-xs text-gray-300">{p.label}</div>
              <p className="text-[11px] text-gray-500 truncate" title={p.description}>
                {p.description}
              </p>
            </div>
            <button
              onClick={() => startEnriching(p.id)}
              disabled={enriching || parentLoading}
              className="px-3 py-1 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded text-xs shrink-0 transition-colors"
            >
              Run
            </button>
          </div>
        ))}

        {upfront?.importance_floor != null && selectedPassId && (() => {
          const m = upfront.passes?.[selectedPassId];
          const majorsDone = m && m.total > 0 && m.done >= m.total;
          return (
            <p className="text-xs text-gray-500">
              Lazy detail is on: only major locations
              {m ? ` (${m.done}/${m.total})` : ''} are detailed upfront
              {majorsDone ? ' — done' : ''}. The rest of the map generates
              silently during play as the story approaches it.
            </p>
          );
        })()}

        {Object.keys(perLayer).length > 1 && (
          <div className="pt-1 space-y-2">
            {Object.entries(perLayer).map(([mid, lp]) => (
              <ProgressBar
                key={mid}
                filled={lp.done || 0}
                total={lp.total || 0}
                label={labelFor(mid)}
              />
            ))}
          </div>
        )}
      </div>

      {reworkAvailable && (
        <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={reworkMode}
            disabled={enriching || parentLoading}
            onChange={(e) => {
              setReworkMode(e.target.checked);
              setSessionIds({});
              setEnrichResults([]);
            }}
            className="accent-purple-500"
          />
          {`Rework finished nodes (regenerate ${selectedSpec ? selectedSpec.label.toLowerCase() : 'this pass'} using current neighbor context)`}
        </label>
      )}

      {!isComplete && (
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-400">Generate:</label>
            <input
              type="range"
              min={1}
              max={genCap}
              value={Math.min(targetCount, genCap)}
              onChange={handleTargetChange}
              disabled={enriching || parentLoading}
              className="flex-1 accent-purple-500 w-24"
            />
            <input
              type="number"
              min={1}
              max={genCap}
              value={targetCount}
              onChange={handleTargetChange}
              disabled={enriching || parentLoading}
              className="w-14 bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs text-center text-gray-200"
            />
          </div>

          {mapList.length > 0 && (
            <div className="flex items-center gap-2">
              <label className="text-xs text-gray-400">Map:</label>
              <select
                value={layerFilter}
                onChange={(e) => {
                  setLayerFilter(e.target.value);
                  setSessionIds({});
                  setEnrichResults([]);
                }}
                disabled={enriching || parentLoading}
                className="bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs text-gray-200"
              >
                <option value="">All maps</option>
                {mapList.map((m) => (
                  <option key={m.id} value={m.id}>{m.label}</option>
                ))}
              </select>
            </div>
          )}

          {!enriching ? (
            <>
              <button
                onClick={() => startEnriching()}
                disabled={parentLoading || isComplete || !selectedSpec}
                title={selectedSpec ? `Run the ${selectedSpec.label.toLowerCase()} pass` : ''}
                className="px-4 py-1.5 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors flex items-center gap-2"
              >
                Start
              </button>
              <button
                onClick={() => startEnriching('all')}
                disabled={parentLoading}
                title="Run every enrichment pass over the remaining nodes in one go"
                className="px-4 py-1.5 bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors flex items-center gap-2"
              >
                Enrich everything
              </button>
            </>
          ) : (
            <button
              onClick={stopEnriching}
              className="px-4 py-1.5 bg-red-700 hover:bg-red-600 rounded-lg text-sm font-medium transition-colors flex items-center gap-2"
            >
              <span className="inline-block w-3 h-3 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              Stop
            </button>
          )}
        </div>
      )}

      {enrichResults.length > 0 && (
        <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-3 max-h-64 overflow-y-auto space-y-2">
          <div className="text-xs text-gray-500 uppercase tracking-wider">Latest Results</div>
          {enrichResults.slice(-30).reverse().map((r, i) => (
            <div key={i} className="text-xs space-y-0.5 border-b border-gray-700/50 pb-2 last:border-0">
              <div className="flex items-center gap-2">
                <span className="text-gray-500 font-mono">{r.node_id}</span>
                {r.failed && <span className="text-red-400 font-medium">{"Failed"}</span>}
                {!r.failed && r.label && <span className="text-amber-400 font-medium">{r.label}</span>}
                {!r.failed && !r.label && r.name && <span className="text-amber-400 font-medium">{r.name}</span>}
                {r.review_fix && <span className="text-sky-400 text-[10px] uppercase">review</span>}
                {r.layer_id && <span className="text-gray-600 text-[10px]">{r.layer_id}</span>}
              </div>
              {r.review_fix && (
                <p className="text-gray-500">
                  was <span className="line-through">{r.old}</span>{r.problem ? ` — ${r.problem}` : ''}
                </p>
              )}
              {!r.failed && r.description && <p className="text-gray-500">{r.description.slice(0, 200)}</p>}
              {r.failed && <p className="text-red-500/70 italic">{"Generation failed after retries"}</p>}
            </div>
          ))}
        </div>
      )}

    </div>
  );
}
