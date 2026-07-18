import { useState, useEffect, useMemo } from 'react';
import { api } from 'api';
import MapRenderer from './WorldBuilder/MapRenderer';
import { normalizeWorldData, breadcrumb } from './lib/mapspace';

// Rendered generically by the host's in-game overlay slot, which passes the
// whole game `state`. We derive the world props from it and render nothing when
// there is no world loaded (so a world-less scenario shows no map).
export default function GameMapOverlay({ state = {} }) {
  const worldData = state.world_data;
  const playerNodeId = state.player_location_node_id;
  // v2 sessions track the player's map id; older saves still carry a layer id
  // (map ids are layer ids for migrated worlds, so the fallback is lossless).
  const playerMapId = state.player_location_map_id || state.player_location_layer_id;
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
  const [activeMapId, setActiveMapId] = useState(null);
  const [focusNodeId, setFocusNodeId] = useState(null);
  const [expanding, setExpanding] = useState(false);
  const [localSite, setLocalSite] = useState(null); // freshly expanded, pre-refresh

  // One shared normalizer handles both world_format 2 (`maps`/`connections`)
  // and legacy (`map_layers`/`map`) payloads.
  const { mapsById, rootMapId, connections } = useMemo(
    () => normalizeWorldData(worldData),
    [worldData],
  );
  const hasMaps = Object.keys(mapsById).length > 0;

  // The player's current node + its interior (if expanded). Node ids are
  // globally unique, so search every map's nodes.
  const playerNode = useMemo(() => {
    if (!hasMaps) return null;
    const nodes = Object.values(mapsById).flatMap((m) => m.nodes || []);
    return nodes.find((n) => n.id === playerNodeId) || null;
  }, [hasMaps, mapsById, playerNodeId]);

  // Legacy site bundle (pre-migration payloads only; migrated worlds carry
  // real interior maps instead).
  const site = localSite?.parent_node_id === playerNodeId
    ? localSite
    : worldData?.site_maps?.[playerNodeId] || null;
  const sitePosition = state.module_data?.wb_worldgen?.site_position;
  // Guard on sitePosition itself: with neither a site position nor a player
  // node id, `undefined === undefined` used to pass and crash on the read.
  const currentSubId = sitePosition && sitePosition.parent_node_id === playerNodeId
    ? sitePosition.sub_location_id
    : null;
  // A child map anchored at the player's node (world_format 2 interiors).
  const playerChildMapId = useMemo(() => {
    if (!playerNodeId) return null;
    const entry = Object.values(mapsById).find((m) => m.anchor_node_id === playerNodeId);
    return entry ? entry.map_id : null;
  }, [mapsById, playerNodeId]);
  // Any NAMED node can be explored in depth — the player decides what
  // deserves a map of its own; nothing is refused on request.
  const canExplore = !site && !playerChildMapId && playerNode && playerNode.name;

  const exploreInterior = async () => {
    if (!playerNodeId || expanding) return;
    setExpanding(true);
    try {
      const res = await api.expandSessionSite(playerNodeId);
      if (res.site) setLocalSite(res.site); // legacy payload
      if (res.map?.map_id) {
        // The world gained a real interior map — jump the view into it.
        setActiveMapId(res.map.map_id);
      }
    } catch (e) {
      console.error('Map expansion failed:', e);
    } finally {
      setExpanding(false);
    }
  };

  // Seed the visible map from the player's map, falling back to the root.
  useEffect(() => {
    if (hasMaps && !activeMapId) {
      setActiveMapId((playerMapId && mapsById[playerMapId] && playerMapId) || rootMapId);
    }
  }, [hasMaps, mapsById, playerMapId, rootMapId, activeMapId]);

  // Focus is transient: it zooms the renderer in on the node, then clears so
  // later map switches reset to their default view as usual.
  const focusOn = (nodeId) => {
    setFocusNodeId(nodeId);
    setTimeout(() => setFocusNodeId(null), 4000);
  };

  const handleMapChange = (targetMapId, nodeId) => {
    setActiveMapId(targetMapId);
    if (nodeId) focusOn(nodeId);
  };

  const crumbs = useMemo(
    () => (activeMapId ? breadcrumb(mapsById, activeMapId) : []),
    [mapsById, activeMapId],
  );

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
          onClick={() => {
            setOpen(true);
            // Open centered + zoomed on the player's current position instead
            // of the default whole-map view.
            if (playerNodeId) focusOn(playerNodeId);
          }}
          className="bg-gray-800/90 border border-gray-600 hover:border-purple-500 rounded-lg px-3 py-2 text-sm text-gray-300 hover:text-purple-300 transition-colors shadow-lg"
        >
          Map
        </button>
      ) : (
        <div className="bg-gray-900/95 border border-gray-700 rounded-xl shadow-2xl overflow-hidden" style={{ width: 360 }}>
          <div className="flex items-center justify-between px-3 py-2 bg-gray-800/80 border-b border-gray-700">
            <span className="text-xs text-gray-400 font-medium">
              World Map
              {crumbs.length > 1 && (
                <span className="text-gray-500 font-normal ml-2">
                  {crumbs.map((m) => m.label || m.map_id).join(' › ')}
                </span>
              )}
            </span>
            <button
              onClick={() => setOpen(false)}
              className="text-gray-500 hover:text-gray-300 text-sm"
            >
              x
            </button>
          </div>
          <div className="h-[268px] w-full">
            {hasMaps && (
              <MapRenderer
                mapsById={mapsById}
                connections={connections}
                rootMapId={rootMapId}
                worldId={worldId}
                activeMapId={activeMapId}
                onMapChange={handleMapChange}
                playerMapId={playerMapId}
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
