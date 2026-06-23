import { useState, useEffect, useMemo } from 'react';
import MapRenderer from './WorldBuilder/MapRenderer';

export default function GameMapOverlay({ worldData, playerNodeId, playerLayerId, revealedNodeIds }) {
  const [open, setOpen] = useState(false);
  const [activeLayerId, setActiveLayerId] = useState(null);

  // Set initial layer from player or first layer
  useEffect(() => {
    if (worldData?.map_layers && !activeLayerId) {
      setActiveLayerId(playerLayerId || worldData.map_layers[0]?.layer_id);
    }
  }, [worldData, playerLayerId]);

  const mapData = useMemo(() => {
    if (!worldData) return null;
    if (worldData.map_layers) {
      return {
        layers: worldData.map_layers,
        connections: worldData.map_connections,
        config: worldData.map_layers[0]?.map?.config || {},
      };
    }
    return {
      nodes: worldData.map?.nodes,
      edges: worldData.map?.edges,
      regions: worldData.map?.regions,
      config: worldData.map?.config,
    };
  }, [worldData]);

  if (!worldData) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50">
      {!open ? (
        <button
          onClick={() => setOpen(true)}
          className="bg-gray-800/90 border border-gray-600 hover:border-purple-500 rounded-lg px-3 py-2 text-sm text-gray-300 hover:text-purple-300 transition-colors shadow-lg"
        >
          Map
        </button>
      ) : (
        <div className="bg-gray-900/95 border border-gray-700 rounded-xl shadow-2xl overflow-hidden" style={{ width: 360, height: 300 }}>
          <div className="flex items-center justify-between px-3 py-2 bg-gray-800/80 border-b border-gray-700">
            <span className="text-xs text-gray-400 font-medium">World Map</span>
            <button
              onClick={() => setOpen(false)}
              className="text-gray-500 hover:text-gray-300 text-sm"
            >
              x
            </button>
          </div>
          <div className="h-[268px] w-full">
            {mapData && (
              <MapRenderer
                {...mapData}
                activeLayerId={activeLayerId}
                onLayerChange={setActiveLayerId}
                fogOfWar={{
                  mode: 'radius',
                  playerNodeId,
                  revealedNodeIds: revealedNodeIds || [],
                  radiusSteps: 1,
                }}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
