import { useState, useEffect, useMemo } from 'react';
import MapRenderer from '../MapRenderer';
import SchemaForm from './SchemaForm';
import { normalizeWorldData } from '../../lib/mapspace';

/**
 * MapStepView — body for procedural map steps.
 * Renders the step's schema fields (e.g. total_nodes) followed by the
 * interactive map for whatever map data currently lives in editedData.
 * Step data may be legacy (layers/connections) or world_format 2 (maps);
 * the shared normalizer handles both. Owns its own active-map / focus-node
 * UI state.
 */
export default function MapStepView({ step, editedData, onFieldChange, disabled, worldId }) {
  const [activeMapId, setActiveMapId] = useState(null);
  const [focusNodeId, setFocusNodeId] = useState(null);

  const normalized = useMemo(() => normalizeWorldData(editedData), [editedData]);
  const hasMaps = Object.keys(normalized.mapsById).length > 0;

  useEffect(() => {
    if (hasMaps && !activeMapId) {
      setActiveMapId(normalized.rootMapId);
    }
  }, [hasMaps, normalized, activeMapId]);

  const handleMapChange = (targetMapId, nodeId) => {
    setActiveMapId(targetMapId);
    if (nodeId) {
      setFocusNodeId(nodeId);
      setTimeout(() => setFocusNodeId(null), 4000);
    }
  };

  const hasMap = editedData && (editedData.nodes || hasMaps);

  return (
    <div className="space-y-4">
      <SchemaForm
        step={step}
        editedData={editedData}
        onFieldChange={onFieldChange}
        disabled={disabled}
      />

      {hasMap && (
        <div className="pt-2">
          {hasMaps ? (
            <MapRenderer
              mapsById={normalized.mapsById}
              connections={normalized.connections}
              rootMapId={normalized.rootMapId}
              activeMapId={activeMapId}
              onMapChange={handleMapChange}
              focusNodeId={focusNodeId}
              worldId={worldId}
            />
          ) : (
            <MapRenderer
              nodes={editedData.nodes}
              edges={editedData.edges}
              regions={editedData.regions}
              roads={editedData.roads}
              config={editedData.config}
              onMapChange={handleMapChange}
              focusNodeId={focusNodeId}
              worldId={worldId}
            />
          )}
        </div>
      )}
    </div>
  );
}
