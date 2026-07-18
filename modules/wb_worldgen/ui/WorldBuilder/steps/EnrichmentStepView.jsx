import { useState, useEffect, useMemo } from 'react';
import MapRenderer from '../MapRenderer';
import EnrichmentPanel from '../EnrichmentPanel';
import { normalizeWorldData } from '../../lib/mapspace';

/**
 * EnrichmentStepView — body for incremental node enrichment steps
 * (labeling / descriptions). Renders the generated map (from the
 * map_generation step) live-merged with in-progress enrichment results,
 * plus the EnrichmentPanel that drives the per-node LLM calls.
 * Owns its own layer/focus state and the live-result buffers.
 */
export default function EnrichmentStepView({
  step,
  state,
  worldState,
  worldId,
  editedData,
  loading,
  enriching,
  onEnrichingChange,
  onEnrich,
  onEnrichCommit,
}) {
  const [activeMapId, setActiveMapId] = useState(null);
  const [focusNodeId, setFocusNodeId] = useState(null);
  const [liveLabels, setLiveLabels] = useState({});
  const [liveDescriptions, setLiveDescriptions] = useState({});
  const [liveLabelDescs, setLiveLabelDescs] = useState({});

  // Legacy (layers/nodes) or world_format 2 (maps) — the normalizer handles
  // both, since step data stays legacy-shaped during world building.
  const mapSourceData = worldState?.steps?.map_generation?.data;
  const normalized = useMemo(() => normalizeWorldData(mapSourceData), [mapSourceData]);
  const hasMaps = Object.keys(normalized.mapsById).length > 0;

  useEffect(() => {
    setLiveLabels({});
    setLiveDescriptions({});
    setLiveLabelDescs({});
    setFocusNodeId(null);
  }, [state?.data]);

  useEffect(() => {
    if (hasMaps && !activeMapId) {
      setActiveMapId(normalized.rootMapId);
    }
  }, [hasMaps, normalized, activeMapId]);

  const handleEnrichResult = (result) => {
    if (result.node_id && result.label) {
      setLiveLabels((prev) => ({ ...prev, [result.node_id]: result.label }));
    }
    if (result.node_id && result.label_description) {
      setLiveLabelDescs((prev) => ({ ...prev, [result.node_id]: result.label_description }));
    }
    if (result.node_id && result.description) {
      setLiveDescriptions((prev) => ({ ...prev, [result.node_id]: result.description }));
    }
  };

  const handleMapChange = (targetMapId, nodeId) => {
    setActiveMapId(targetMapId);
    if (nodeId) {
      setFocusNodeId(nodeId);
      setTimeout(() => setFocusNodeId(null), 4000);
    }
  };

  // Live-merge in-progress enrichment results into the normalized maps.
  const mergeInto = (nodes) =>
    (nodes || []).map((n) => ({
      ...n,
      name: liveLabels[n.id] || n.name,
      label_description: liveLabelDescs[n.id] || n.label_description,
      description: liveDescriptions[n.id] || n.description,
    }));

  const getEnrichedMapsById = () => {
    const hasLive = Object.keys(liveLabels).length > 0
      || Object.keys(liveDescriptions).length > 0
      || Object.keys(liveLabelDescs).length > 0;
    if (!hasLive) return normalized.mapsById;
    const out = {};
    Object.entries(normalized.mapsById).forEach(([id, m]) => {
      out[id] = { ...m, nodes: mergeInto(m.nodes) };
    });
    return out;
  };

  const hasMapData = mapSourceData && (mapSourceData.nodes || hasMaps);

  return (
    <div className="space-y-4">
      {hasMapData && (
        // Explicit height: the renderer fills whatever box it's given.
        <div className="pt-2 h-[560px]">
          {hasMaps ? (
            <MapRenderer
              mapsById={getEnrichedMapsById()}
              connections={normalized.connections}
              rootMapId={normalized.rootMapId}
              activeMapId={activeMapId}
              onMapChange={handleMapChange}
              focusNodeId={focusNodeId}
              worldId={worldId}
            />
          ) : (
            <MapRenderer
              nodes={mergeInto(mapSourceData.nodes)}
              edges={mapSourceData.edges}
              regions={mapSourceData.regions}
              roads={mapSourceData.roads}
              config={mapSourceData.config}
              onMapChange={handleMapChange}
              focusNodeId={focusNodeId}
              worldId={worldId}
            />
          )}
        </div>
      )}

      <EnrichmentPanel
        stepId={step.id}
        stepLabel={step.label}
        data={editedData}
        state={state}
        worldId={worldId}
        onEnrich={onEnrich}
        onCommit={onEnrichCommit}
        loading={loading}
        enriching={enriching}
        onEnrichingChange={onEnrichingChange}
        onResult={handleEnrichResult}
      />
    </div>
  );
}
