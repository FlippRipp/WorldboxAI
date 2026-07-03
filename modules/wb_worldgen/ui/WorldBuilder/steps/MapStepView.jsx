import { useState, useEffect } from 'react';
import MapRenderer from '../MapRenderer';
import SchemaForm from './SchemaForm';

/**
 * MapStepView — body for procedural map steps.
 * Renders the step's schema fields (e.g. total_nodes) followed by the
 * interactive map for whatever map data currently lives in editedData.
 * Owns its own active-layer / focus-node UI state.
 */
export default function MapStepView({ step, editedData, onFieldChange, disabled, worldId }) {
  const [activeLayerId, setActiveLayerId] = useState(null);
  const [focusNodeId, setFocusNodeId] = useState(null);

  useEffect(() => {
    if (editedData?.layers?.length > 0 && !activeLayerId) {
      setActiveLayerId(editedData.layers[0].layer_id);
    }
  }, [editedData, activeLayerId]);

  const navigateToLayer = (targetLayerId, nodeId) => {
    setActiveLayerId(targetLayerId);
    setFocusNodeId(nodeId);
    setTimeout(() => setFocusNodeId(null), 4000);
  };

  const hasMap = editedData && (editedData.nodes || editedData.layers);

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
          {editedData.layers ? (
            <MapRenderer
              layers={editedData.layers}
              connections={editedData.connections}
              activeLayerId={activeLayerId}
              onLayerChange={setActiveLayerId}
              config={editedData.config}
              navigateToLayer={navigateToLayer}
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
              navigateToLayer={navigateToLayer}
              focusNodeId={focusNodeId}
              worldId={worldId}
            />
          )}
        </div>
      )}
    </div>
  );
}
