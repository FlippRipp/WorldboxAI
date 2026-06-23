import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../../lib/api';

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

export default function EnrichmentPanel({ stepId, stepLabel, data, state, worldId, onEnrich, onCommit, loading: parentLoading, enriching, onEnrichingChange, onResult }) {
  const [targetCount, setTargetCount] = useState(30);
  const [enrichResults, setEnrichResults] = useState([]);
  const [progress, setProgress] = useState(null);
  const [layerFilter, setLayerFilter] = useState('');
  const [labelSessionIds, setLabelSessionIds] = useState([]);
  const [descSessionIds, setDescSessionIds] = useState([]);
  const abortRef = useRef(false);

  const isLabeling = stepId === 'node_labeling';

  // Fetch progress on mount and when results change
  const fetchProgress = useCallback(async () => {
    if (!worldId) return;
    try {
      const p = await api.enrichProgress(worldId, layerFilter || null);
      setProgress(isLabeling ? p.labeling : p.descriptions);
    } catch (e) {
      console.error('Progress fetch failed:', e);
    }
  }, [worldId, layerFilter, isLabeling]);

  useEffect(() => {
    fetchProgress();
  }, [fetchProgress]);

  useEffect(() => {
    return () => { abortRef.current = true; };
  }, []);

  const perLayer = progress?.per_layer || {};
  const totalNodes = progress?.total_nodes || 0;
  const totalLabeled = progress?.total_labeled || progress?.total_described || 0;
  const remaining = totalNodes - totalLabeled;

  const layers = data?.layers || [];
  const isComplete = remaining <= 0 && totalNodes > 0;

  const handleTargetChange = (e) => {
    setTargetCount(Math.max(1, parseInt(e.target.value) || 1));
  };

  const startEnriching = async () => {
    if (!worldId || targetCount <= 0 || isComplete) return;
    abortRef.current = false;
    onEnrichingChange(true);
    setEnrichResults([]);

    const newResults = [];
    const newLabeled = [...labelSessionIds];
    const newDescribed = [...descSessionIds];
    let generated = 0;

    while (generated < targetCount && !abortRef.current) {
      let result;
      try {
        if (isLabeling) {
          result = await api.enrichLabelNext(worldId, layerFilter || null, newLabeled);
        } else {
          result = await api.enrichDescribeNext(worldId, layerFilter || null, newDescribed);
        }
      } catch (e) {
        console.error('Enrich step failed:', e);
        break;
      }

      if (result.complete) {
        setProgress({
          per_layer: result.per_layer,
          total_nodes: result.total_nodes,
          total_labeled: result.total_labeled,
        });
        break;
      }

      if (result.failed_node_ids && result.failed_node_ids.length > 0) {
        newResults.push({
          node_id: result.failed_node_ids[0],
          label: null,
          name: null,
          description: null,
          layer_id: result.layer_id,
          failed: true,
        });
        generated++;
        setEnrichResults([...newResults]);
        setProgress({
          per_layer: result.per_layer,
          total_nodes: result.total_nodes,
          total_labeled: result.total_labeled,
        });
        continue;
      }

      if (result.node_id) {
        if (isLabeling) {
          newLabeled.push(result.node_id);
        } else {
          newDescribed.push(result.node_id);
        }
        newResults.push({
          node_id: result.node_id,
          label: result.label,
          name: result.label,
          description: result.description,
          layer_id: result.layer_id,
        });
        generated++;
        setEnrichResults([...newResults]);
        setLabelSessionIds([...newLabeled]);
        setDescSessionIds([...newDescribed]);
        onResult?.({
          node_id: result.node_id,
          label: result.label,
          description: result.description,
          layer_id: result.layer_id,
        });
        setProgress({
          per_layer: result.per_layer,
          total_nodes: result.total_nodes,
          total_labeled: result.total_labeled,
        });
      }

      // Delay between calls to avoid overwhelming the LLM service
      await new Promise((r) => setTimeout(r, 300));
    }

    onEnrichingChange(false);
  };

  const stopEnriching = () => {
    abortRef.current = true;
    onEnrichingChange(false);
  };

  const handleCommit = async () => {
    try {
      await api.enrichCommit(worldId, stepId);
      onCommit?.(stepId);
    } catch (e) {
      console.error('Commit failed:', e);
    }
  };

  return (
    <div className="space-y-4 pt-2">
      <div className="bg-gray-800/60 border border-gray-700 rounded-lg p-4 space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-gray-300">
            {totalNodes > 0
              ? `${totalLabeled}/${totalNodes} enriched ${isComplete ? '- Complete' : ''}`
              : 'Loading progress...'}
          </span>
        </div>

        {layers.length > 0 && layers.map((layer) => {
          const lp = perLayer[layer.layer_id] || { done: 0, total: 0 };
          return (
            <ProgressBar
              key={layer.layer_id}
              filled={lp.done || 0}
              total={lp.total || 0}
              label={layer.name}
            />
          );
        })}

        {!layers.length && Object.entries(perLayer).length > 0 &&
          Object.entries(perLayer).map(([lid, lp]) => (
            <ProgressBar key={lid} filled={lp.done || 0} total={lp.total || 0} label={lid} />
          ))
        }

        {!layers.length && Object.keys(perLayer).length === 0 && totalNodes > 0 && (
          <ProgressBar filled={totalLabeled} total={totalNodes} label="Total" />
        )}
      </div>

      {!isComplete && (
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-400">Generate:</label>
            <input
              type="range"
              min={1}
              max={remaining || 1}
              value={Math.min(targetCount, remaining || 1)}
              onChange={handleTargetChange}
              disabled={enriching || parentLoading}
              className="flex-1 accent-purple-500 w-24"
            />
            <input
              type="number"
              min={1}
              max={remaining || 1}
              value={targetCount}
              onChange={handleTargetChange}
              disabled={enriching || parentLoading}
              className="w-14 bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs text-center text-gray-200"
            />
          </div>

          {layers.length > 0 && (
            <div className="flex items-center gap-2">
              <label className="text-xs text-gray-400">Layer:</label>
              <select
                value={layerFilter}
                onChange={(e) => {
                  setLayerFilter(e.target.value);
                  setLabelSessionIds([]);
                  setDescSessionIds([]);
                  setEnrichResults([]);
                }}
                disabled={enriching || parentLoading}
                className="bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs text-gray-200"
              >
                <option value="">All layers</option>
                {layers.map((l) => (
                  <option key={l.layer_id} value={l.layer_id}>{l.name}</option>
                ))}
              </select>
            </div>
          )}

          {!enriching ? (
            <button
              onClick={startEnriching}
              disabled={parentLoading || isComplete}
              className="px-4 py-1.5 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors flex items-center gap-2"
            >
              Start
            </button>
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
                {r.layer_id && <span className="text-gray-600 text-[10px]">{r.layer_id}</span>}
              </div>
              {!r.failed && r.description && <p className="text-gray-500">{r.description.slice(0, 200)}</p>}
              {r.failed && <p className="text-red-500/70 italic">{"Generation failed after retries"}</p>}
            </div>
          ))}
        </div>
      )}

      <div className="flex gap-2">
        <button
          onClick={handleCommit}
          disabled={parentLoading || totalLabeled === 0}
          className="px-3 py-1.5 bg-green-700 hover:bg-green-600 disabled:opacity-50 rounded text-xs font-medium transition-colors"
        >
          Commit Results
        </button>
      </div>
    </div>
  );
}
