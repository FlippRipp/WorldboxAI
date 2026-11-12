import { useState, useEffect, useMemo, useRef } from 'react';
import { api } from 'api';
import { normalizeWorldData, breadcrumb } from './lib/mapspace';

let CachedMapRenderer = null;

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
    if (!travel) return null;
    // Legacy turn-based record (pre time-based travel; re-plans on next turn).
    if (travel.route && travel.leg_index != null) {
      const from = travel.route[travel.leg_index];
      const to = travel.route[travel.leg_index + 1];
      if (!from || !to) return null;
      const frac = travel.leg_distance ? Math.min(travel.leg_progress / travel.leg_distance, 1) : 0;
      return { fromNodeId: from, toNodeId: to, frac };
    }
    // Time-based journey: walk the itinerary by the elapsed-minutes fraction
    // (mirrors wbruntime/routing.advance_position).
    const itinerary = travel.itinerary;
    if (!itinerary?.segments) return null;
    const eta = Math.max(1, travel.eta_minutes || 1);
    let remaining = (itinerary.ee_total || 0)
      * Math.min(1, (travel.minutes_traveled || 0) / eta);
    for (const seg of itinerary.segments) {
      if (seg.kind === 'connection') {
        if (remaining >= seg.ee) { remaining -= seg.ee; continue; }
        return null; // aboard the connection — the marker stays at the near node
      }
      for (let i = 0; i < (seg.leg_ee || []).length; i++) {
        const ee = seg.leg_ee[i];
        if (remaining >= ee) { remaining -= ee; continue; }
        return { fromNodeId: seg.nodes[i], toNodeId: seg.nodes[i + 1], frac: ee ? remaining / ee : 1 };
      }
    }
    return null;
  }, [travel]);

  const [open, setOpen] = useState(false);
  const [MapRendererComp, setMapRendererComp] = useState(() => CachedMapRenderer);
  const [activeMapId, setActiveMapId] = useState(null);
  const [focusNodeId, setFocusNodeId] = useState(null);
  const [expanding, setExpanding] = useState(false);
  const [localSite, setLocalSite] = useState(null); // freshly expanded, pre-refresh

  // Lazy-load the heavy 1,500-line MapRenderer only when the map overlay is opened.
  useEffect(() => {
    if (!open || MapRendererComp) return;
    let cancelled = false;
    const loader = typeof window !== 'undefined' ? window.__wbLoadModuleComponent : null;
    if (loader) {
      loader('wb_worldgen', 'ui/WorldBuilder/MapRenderer.jsx')
        .then((Comp) => {
          CachedMapRenderer = Comp;
          if (!cancelled) setMapRendererComp(() => Comp);
        })
        .catch((err) => console.error('Failed to lazy-load MapRenderer:', err));
    }
    return () => { cancelled = true; };
  }, [open, MapRendererComp]);

  // Trap Android Back while the map overlay is open.
  useEffect(() => {
    if (!open || typeof window === 'undefined' || !window.__wbRegisterBackHandler) return undefined;
    return window.__wbRegisterBackHandler(() => setOpen(false));
  }, [open]);

  // One shared normalizer handles both world_format 2 (`maps`/`connections`)
  // and legacy (`map_layers`/`map`) payloads. Hidden (undiscovered) passages
  // never render on the player's map — discover_passage flips the flag in
  // the session copy, so found ways appear on their own.
  const { mapsById, rootMapId, connections } = useMemo(() => {
    const normalized = normalizeWorldData(worldData);
    return {
      ...normalized,
      connections: (normalized.connections || []).filter((c) => !c.hidden),
    };
  }, [worldData]);
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

  // Allow the mobile Header 1-tap Map icon to toggle this overlay.
  useEffect(() => {
    if (!worldData || typeof window === 'undefined') return undefined;
    const onToggle = () => {
      setOpen((prev) => {
        const next = !prev;
        if (next && playerNodeId) focusOn(playerNodeId);
        return next;
      });
    };
    window.addEventListener('wb:toggle-map', onToggle);
    return () => window.removeEventListener('wb:toggle-map', onToggle);
  }, [worldData, playerNodeId]);

  // Follow the player across maps: a server-driven move to another map —
  // story travel crossing a boundary, /teleport, an undo rolling the player
  // back outside — switches the displayed map to where the player now is.
  // Keyed on the CHANGE of playerMapId (not its value), so manually browsing
  // other maps is never overridden while the player stays put.
  const followedMapRef = useRef(playerMapId);
  useEffect(() => {
    if (!playerMapId || !mapsById[playerMapId]) return;
    if (followedMapRef.current === playerMapId) return;
    followedMapRef.current = playerMapId;
    setActiveMapId(playerMapId);
    if (playerNodeId) focusOn(playerNodeId);
  }, [playerMapId, mapsById, playerNodeId]);

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
    // On < 1024px the mobile Header provides a 1-tap Map button, so hide the
    // floating trigger pill there. When opened, display a responsive card on
    // phones and a 360px bottom-right panel on larger screens.
    <>
      {!open ? (
        <div className="hidden lg:block fixed right-4 bottom-4 z-50">
          <button
            onClick={() => {
              setOpen(true);
              if (playerNodeId) focusOn(playerNodeId);
            }}
            className="min-h-10 bg-gray-800/90 border border-gray-600 hover:border-purple-500 rounded-lg px-3 py-2 text-sm text-gray-300 hover:text-purple-300 transition-colors shadow-lg"
          >
            Map
          </button>
        </div>
      ) : (
        <div className="fixed inset-x-3 top-14 sm:inset-auto sm:right-4 sm:bottom-4 sm:w-[360px] z-50 bg-gray-900/95 border border-gray-700 rounded-xl shadow-2xl overflow-hidden">
          <div className="flex items-center justify-between pl-3 pr-1 py-1 bg-gray-800/80 border-b border-gray-700 gap-2">
            <span className="text-xs text-gray-400 font-medium min-w-0 truncate">
              World Map
              {crumbs.length > 1 && (
                <span className="text-gray-500 font-normal ml-2">
                  {crumbs.map((m) => m.label || m.map_id).join(' › ')}
                </span>
              )}
            </span>
            <button
              onClick={() => setOpen(false)}
              className="min-h-10 min-w-10 flex items-center justify-center text-gray-400 hover:text-gray-200 text-lg leading-none shrink-0"
              aria-label="Close map"
            >
              &times;
            </button>
          </div>
          <div className="h-[268px] w-full">
            {hasMaps && MapRendererComp ? (
              <MapRendererComp
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
                  playerNodeId,
                  revealedNodeIds: revealedNodeIds || [],
                }}
              />
            ) : hasMaps ? (
              <div className="h-full w-full flex items-center justify-center text-xs text-gray-500">
                Loading map...
              </div>
            ) : null}
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
    </>
  );
}
