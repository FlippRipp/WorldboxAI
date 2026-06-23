import { useState, useEffect } from 'react';
import MapRenderer from '../MapRenderer';
import EnrichmentPanel from '../EnrichmentPanel';

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
  const [activeLayerId, setActiveLayerId] = useState(null);
  const [focusNodeId, setFocusNodeId] = useState(null);
  const [liveLabels, setLiveLabels] = useState({});
  const [liveDescriptions, setLiveDescriptions] = useState({});
  const [liveLabelDescs, setLiveLabelDescs] = useState({});

  const mapSourceData = worldState?.steps?.map_generation?.data;

  useEffect(() => {
    setLiveLabels({});
    setLiveDescriptions({});
    setLiveLabelDescs({});
    setFocusNodeId(null);
  }, [state?.data]);

  useEffect(() => {
    if (mapSourceData?.layers?.length > 0 && !activeLayerId) {
      setActiveLayerId(mapSourceData.layers[0].layer_id);
    }
  }, [mapSourceData, activeLayerId]);

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

  const navigateToLayer = (targetLayerId, nodeId) => {
    setActiveLayerId(targetLayerId);
    setFocusNodeId(nodeId);
    setTimeout(() => setFocusNodeId(null), 4000);
  };

  const getEnrichedMapData = () => {
    const source = mapSourceData;
    if (!source) return null;
    const hasLiveL = Object.keys(liveLabels).length > 0;
    const hasLiveD = Object.keys(liveDescriptions).length > 0;
    if (!hasLiveL && !hasLiveD) return source;

    const mergeInto = (nodes) =>
      nodes.map((n) => ({
        ...n,
        name: liveLabels[n.id] || n.name,
        label_description: liveLabelDescs[n.id] || n.label_description,
        description: liveDescriptions[n.id] || n.description,
      }));

    if (source.layers) {
      return {
        ...source,
        layers: source.layers.map((layer) => ({
          ...layer,
          map: {
            ...layer.map,
            nodes: mergeInto(layer.map?.nodes || []),
          },
        })),
      };
    }
    if (source.nodes) {
      return { ...source, nodes: mergeInto(source.nodes) };
    }
    return source;
  };

  const enriched = getEnrichedMapData();
  const hasMapData = mapSourceData && (mapSourceData.nodes || mapSourceData.layers);

  return (
    <div className="space-y-4">
      {hasMapData && (
        <div className="pt-2">
          {enriched.layers ? (
            <MapRenderer
              layers={enriched.layers}
              connections={enriched.connections}
              activeLayerId={activeLayerId}
              onLayerChange={setActiveLayerId}
              config={enriched.config}
              navigateToLayer={navigateToLayer}
              focusNodeId={focusNodeId}
            />
          ) : (
            <MapRenderer
              nodes={enriched.nodes}
              edges={enriched.edges}
              regions={enriched.regions}
              config={enriched.config}
              navigateToLayer={navigateToLayer}
              focusNodeId={focusNodeId}
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
