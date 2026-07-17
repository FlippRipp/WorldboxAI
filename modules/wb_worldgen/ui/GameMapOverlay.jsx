import { useState, useEffect, useMemo } from 'react';
import { api } from 'api';
import MapRenderer from './WorldBuilder/MapRenderer';

// Node types whose interiors can be expanded into districts/venues (mirrors
// the backend's expandable set).
const EXPANDABLE_TYPES = new Set(['city', 'settlement', 'port', 'stronghold']);

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
  const [expanding, setExpanding] = useState(false);
  const [localSite, setLocalSite] = useState(null); // freshly expanded, pre-refresh

  // The player's current node + its interior (if expanded).
  const playerNode = useMemo(() => {
    if (!worldData) return null;
    const nodes = worldData.map_layers
      ? worldData.map_layers.flatMap((l) => l.map?.nodes || [])
      : worldData.map?.nodes || [];
    return nodes.find((n) => n.id === playerNodeId) || null;
  }, [worldData, playerNodeId]);

  const site = localSite?.parent_node_id === playerNodeId
    ? localSite
    : worldData?.site_maps?.[playerNodeId] || null;
  const sitePosition = state.module_data?.wb_worldgen?.site_position;
  const currentSubId = sitePosition?.parent_node_id === playerNodeId
    ? sitePosition.sub_location_id
    : null;
  const canExplore = !site && playerNode && playerNode.name
    && EXPANDABLE_TYPES.has(playerNode.type) && (playerNode.importance ?? 0) >= 6;

  const exploreInterior = async () => {
    if (!playerNodeId || expanding) return;
    setExpanding(true);
    try {
      const res = await api.expandSessionSite(playerNodeId);
      setLocalSite(res.site);
    } catch (e) {
      console.error('Site expansion failed:', e);
    } finally {
      setExpanding(false);
    }
  };

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
        <div className="bg-gray-900/95 border border-gray-700 rounded-xl shadow-2xl overflow-hidden" style={{ width: 360 }}>
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
          {site && (
            <div className="border-t border-gray-700 px-3 py-2 max-h-40 overflow-y-auto">
              <div className="text-xs text-purple-300 font-medium mb-1">
                Inside {site.name || playerNode?.name}
              </div>
              {site.layout_summary && (
                <p className="text-[11px] text-gray-400 mb-1">{site.layout_summary}</p>
              )}
              <ul className="space-y-0.5">
                {(site.sub_locations || []).map((sub) => (
                  <li
                    key={sub.id}
                    className={`text-[11px] ${sub.id === currentSubId
                      ? 'text-purple-200 bg-purple-900/40 rounded px-1 -mx-1'
                      : 'text-gray-300'}`}
                  >
                    <span className={sub.id === currentSubId ? 'text-purple-300 font-medium' : 'text-amber-400'}>
                      {sub.name}
                    </span>
                    <span className="text-gray-500"> ({sub.type})</span>
                    {sub.id === currentSubId && <span className="text-purple-400"> ● here</span>}
                    {sub.description && (
                      <span className="text-gray-500"> — {sub.description.slice(0, 90)}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {canExplore && (
            <div className="border-t border-gray-700 px-3 py-2">
              <button
                onClick={exploreInterior}
                disabled={expanding}
                className="w-full px-3 py-1.5 bg-purple-700 hover:bg-purple-600 disabled:opacity-50 rounded-lg text-xs font-medium text-gray-100 transition-colors"
              >
                {expanding
                  ? 'Detailing this place...'
                  : `Explore ${playerNode?.name} in detail`}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
