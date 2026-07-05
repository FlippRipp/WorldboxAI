import { useState, useEffect, useMemo } from 'react';
import MapRenderer from './WorldBuilder/MapRenderer';

// Rendered generically by the host's in-game overlay slot, which passes the
// whole game `state`. We derive the world props from it and render nothing when
// there is no world loaded (so a world-less scenario shows no map).
export default function GameMapOverlay({ state = {} }) {
  const worldData = state.world_data;
  const playerNodeId = state.player_location_node_id;
  const playerLayerId = state.player_location_layer_id;
  const revealedNodeIds = state.revealed_node_ids || [];
  // Gradual travel: while the player is between nodes, wb_worldgen keeps a
  // journey record in its module_data; the renderer shows the marker mid-edge.
  const travel = state.module_data?.wb_worldgen?.travel;
  const playerTravel = useMemo(() => {
    if (!travel?.route || travel.leg_index == null) return null;
    const from = travel.route[travel.leg_index];
    const to = travel.route[travel.leg_index + 1];
    if (!from || !to) return null;
    const frac = travel.leg_distance ? Math.min(travel.leg_progress / travel.leg_distance, 1) : 0;
    return { fromNodeId: from, toNodeId: to, frac };
  }, [travel]);

  const [open, setOpen] = useState(false);
  const [activeLayerId, setActiveLayerId] = useState(null);
  const [focusNodeId, setFocusNodeId] = useState(null);

  // Set initial layer from player or first layer
  useEffect(() => {
    if (worldData?.map_layers && !activeLayerId) {
      setActiveLayerId(playerLayerId || worldData.map_layers[0]?.layer_id);
    }
  }, [worldData, playerLayerId]);

  const navigateToLayer = (targetLayerId, nodeId) => {
    setActiveLayerId(targetLayerId);
    setFocusNodeId(nodeId);
    setTimeout(() => setFocusNodeId(null), 4000);
  };

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
      roads: worldData.map?.roads,
      config: worldData.map?.config,
    };
  }, [worldData]);

  const worldId = worldData?.world_id || worldData?.id || null;

  if (!worldData) return null;

  return (
    // On mobile the bottom-right corner is occupied by the chat composer's
    // send button, which hides this control; anchor it under the header
    // (top-right) on small screens and restore the desktop bottom-right corner
    // from `sm` up, where the centered composer leaves that gutter empty.
    <div className="fixed right-4 z-50 top-16 sm:top-auto sm:bottom-4">
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
                worldId={worldId}
                activeLayerId={activeLayerId}
                onLayerChange={setActiveLayerId}
                navigateToLayer={navigateToLayer}
                focusNodeId={focusNodeId}
                playerTravel={playerTravel}
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
