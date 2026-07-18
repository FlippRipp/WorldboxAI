import { useState, useMemo, useCallback, useEffect, useRef } from 'react';
import { Delaunay } from 'd3-delaunay';
import { normalizeWorldData, connectionsByNode, breadcrumb, childrenByAnchor } from '../lib/mapspace';

const TYPE_COLORS = {
  settlement: '#f59e0b',
  landmark: '#3b82f6',
  crossroads: '#6b7280',
  wilderness: '#22c55e',
};

const CONNECTION_COLORS = {
  dungeon_entrance: '#ef4444',
  cave_entrance: '#78716c',
  cave_mouth: '#78716c',
  port: '#06b6d4',
  portal: '#a855f7',
  rift: '#ec4899',
  staircase: '#84cc16',
  bridge: '#eab308',
};

const LAYER_ICONS = {
  surface: '\u2601',
  underground: '\u26F0',
  sky: '\u2601',
  ocean: '\u2248',
  continent: '\u25C9',
};

const REGION_STROKE_COLORS = [
  'rgba(139,92,246,0.45)',
  'rgba(59,130,246,0.45)',
  'rgba(239,68,68,0.45)',
  'rgba(34,197,94,0.45)',
  'rgba(251,191,36,0.45)',
  'rgba(236,72,153,0.45)',
  'rgba(14,165,233,0.45)',
  'rgba(168,85,247,0.45)',
];

function getImportanceRadius(imp, scale) {
  const base = 3;
  const maxExtra = 9;
  const frac = (imp - 1) / 9;
  return (base + frac * maxExtra) * scale;
}

function renderDescriptionWithLinks(text) {
  if (!text) return null;
  const parts = text.split(/(\$\{link_[^}]+\})/g);
  return parts.map((part, i) => {
    if (part.startsWith('${link_')) {
      const inner = part.slice(7, -1);
      const [nodeId, ...rest] = inner.split('|');
      const label = rest.join('|') || nodeId;
      return (
        <span key={i} className="text-amber-400 font-medium">
          {label}
        </span>
      );
    }
    return <span key={i}>{part}</span>;
  });
}

function NodeShape({ cx, cy, r, color, isHovered, isPopulated, isConnection, connectionType, scale = 1 }) {
  const stroke = (isHovered ? 2 : 0.5) * scale;
  if (isConnection) {
    const connColor = CONNECTION_COLORS[connectionType] || '#8b5cf6';
    const s = r * 1.4;
    const points = `${cx},${cy - s} ${cx + s},${cy} ${cx},${cy + s} ${cx - s},${cy}`;
    return (
      <polygon
        points={points}
        fill={connColor}
        stroke={isHovered ? '#fff' : 'rgba(0,0,0,0.3)'}
        strokeWidth={stroke}
        opacity={isPopulated ? 0.95 : 0.55}
      />
    );
  }
  return (
    <circle
      cx={cx}
      cy={cy}
      r={r}
      fill={color}
      stroke={isHovered ? '#fff' : 'rgba(0,0,0,0.3)'}
      strokeWidth={stroke}
      opacity={isPopulated ? 0.95 : 0.55}
    />
  );
}

const INTERLAYER_TYPES = new Set([
  'dungeon_entrance', 'port', 'portal', 'cave_entrance',
  'cave_mouth', 'rift', 'staircase', 'bridge',
]);

// Deepest zoom-in as a multiple of the fitted whole-map view. Zoom-out is
// capped at 1 (the whole map exactly fits) — zooming further out only shows
// empty space around the map.
const MAX_ZOOM = 40;

// Screen-pixel multiplier for node markers and their labels: 1 reproduces the
// pre-responsive desktop sizing (an importance-1 node = 3px radius, 8px label
// text); 2 doubles everything for readability, especially on touch screens.
const UI_SCALE = 2;

export default function MapRenderer({ nodes, edges, regions, config, layers, connections, activeLayerId, onLayerChange, mapsById, activeMapId, onMapChange, rootMapId, playerMapId, fogOfWar, navigateToLayer, focusNodeId, worldId, roads, playerTravel }) {
  const [hoveredNode, setHoveredNode] = useState(null);
  const [hoveredRegion, setHoveredRegion] = useState(null);
  const [selectedNode, setSelectedNode] = useState(null);
  const [viewBox, setViewBox] = useState({ x: 0, y: 0, w: 800, h: 500 });
  const [dragging, setDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const [isFullscreen, setIsFullscreen] = useState(false);
  const mapContainerRef = useRef(null);
  const wrapperRef = useRef(null);
  // Active touch gesture: {mode:'pan', lastX, lastY} or {mode:'pinch', lastDist}.
  const touchStateRef = useRef(null);
  // Measured on-screen size of the map container. The container fills whatever
  // box the embedding view gives it (popup, builder step, fullscreen), and the
  // viewBox is derived from this so gesture math always matches what's visible.
  const [containerSize, setContainerSize] = useState({ w: 0, h: 0 });
  // Node-info details visibility: collapsed by default in small views (where
  // the description would crowd out the map), expanded when there's room.
  // A manual toggle overrides the size-based default until unmounted.
  const [infoExpandedOverride, setInfoExpandedOverride] = useState(null);
  const infoExpanded = infoExpandedOverride ?? containerSize.h >= 300;

  // Normalize the map inputs: new-style callers pass `mapsById` (v2 MapRecords
  // keyed by map_id) + v2 `connections`; legacy callers pass `layers` (array of
  // {layer_id, name, layer_type, map}) + map_connections-shaped `connections`,
  // adapted through the shared normalizer. Null when called flat (bare
  // nodes/edges props), which keeps working exactly as before.
  const normalized = useMemo(() => {
    if (mapsById && Object.keys(mapsById).length) {
      return {
        mapsById,
        rootMapId: rootMapId
          || Object.values(mapsById).find((m) => m.parent_map_id == null)?.map_id
          || Object.keys(mapsById)[0],
        connections: connections || [],
      };
    }
    if (layers && layers.length) {
      return normalizeWorldData({ map_layers: layers, map_connections: connections });
    }
    return null;
  }, [mapsById, rootMapId, layers, connections]);

  const hasMaps = !!normalized;
  const currentMapId = activeMapId || activeLayerId || normalized?.rootMapId || null;
  const activeMap = hasMaps
    ? normalized.mapsById[currentMapId] || normalized.mapsById[normalized.rootMapId]
    : null;

  const activeNodes = useMemo(() => {
    if (!hasMaps) return nodes || [];
    return activeMap?.nodes || [];
  }, [hasMaps, activeMap, nodes]);

  const activeEdges = useMemo(() => {
    if (!hasMaps) return edges || [];
    return activeMap?.edges || [];
  }, [hasMaps, activeMap, edges]);

  const activeRegions = useMemo(() => {
    if (!hasMaps) return regions || [];
    return activeMap?.regions || [];
  }, [hasMaps, activeMap, regions]);

  const activeRoads = useMemo(() => {
    if (!hasMaps) return roads || [];
    return activeMap?.roads || [];
  }, [hasMaps, activeMap, roads]);

  const effectiveConfig = (hasMaps ? activeMap?.config : config) || config;

  // Terrain background image. Only world-surface maps have a raster: maps
  // produced by other generators (site interiors etc.) skip the fetch. The
  // raster is keyed by the legacy layer id when the map was migrated.
  const terrainImageUrl = useMemo(() => {
    if (!worldId) return null;
    if (hasMaps) {
      if (!activeMap) return null;
      if (activeMap.generator_id !== undefined && activeMap.generator_id !== 'world_map') return null;
      const layerId = activeMap.legacy_layer_id || activeMap.map_id || 'main';
      return `/api/world/${encodeURIComponent(worldId)}/terrain/${encodeURIComponent(layerId)}/biome`;
    }
    return `/api/world/${encodeURIComponent(worldId)}/terrain/main/biome`;
  }, [worldId, hasMaps, activeMap]);

  const nodeAssignments = useMemo(() => {
    if (!activeRegions || !activeRegions.length) return {};
    const map = {};
    activeRegions.forEach((r, i) => {
      (r.node_ids || []).forEach((nid) => {
        map[nid] = i;
      });
    });
    return map;
  }, [activeRegions]);

  // Layout: coordinate transforms
  const mapLayout = useMemo(() => {
    const pad = 45;
    const viewW = 800;
    const viewH = 500;
    const mw = effectiveConfig?.map_width || 1000;
    const mh = effectiveConfig?.map_height || 1000;
    const s = Math.min((viewW - pad * 2) / mw, (viewH - pad * 2) / mh);
    return { pad, viewW, viewH, scale: s, mapW: mw, mapH: mh };
  }, [effectiveConfig]);

  const sx = useCallback((x) => mapLayout.pad + x * mapLayout.scale, [mapLayout]);
  const sy = useCallback((y) => mapLayout.pad + y * mapLayout.scale, [mapLayout]);

  // Node ID -> index in activeNodes
  const nodeIndex = useMemo(() => {
    const idx = {};
    activeNodes.forEach((n, i) => { idx[n.id] = i; });
    return idx;
  }, [activeNodes]);

  // Player marker: at the current node, or interpolated along the current
  // travel leg while a gradual journey is underway. Null when the player (or
  // their map) isn't the one being displayed.
  const playerMarker = useMemo(() => {
    if (playerMapId != null && hasMaps && activeMap && playerMapId !== activeMap.map_id) {
      return null;
    }
    const byId = {};
    activeNodes.forEach((n) => { byId[n.id] = n; });
    if (playerTravel) {
      const a = byId[playerTravel.fromNodeId];
      const b = byId[playerTravel.toNodeId];
      if (a && b) {
        const t = Math.max(0, Math.min(playerTravel.frac ?? 0, 1));
        return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, traveling: true, from: a, to: b };
      }
    }
    const here = fogOfWar?.playerNodeId ? byId[fogOfWar.playerNodeId] : null;
    if (here) return { x: here.x, y: here.y, traveling: false };
    return null;
  }, [activeNodes, playerTravel, fogOfWar, playerMapId, hasMaps, activeMap]);

  // Voronoi region polygons (fill all cells, stroke only outer boundary)
  const voronoiRegions = useMemo(() => {
    if (!activeNodes.length || !activeRegions.length || mapLayout.scale <= 0) return [];

    const points = activeNodes.map((n) => [n.x, n.y]);
    const delaunay = Delaunay.from(points);
    const voronoi = delaunay.voronoi([0, 0, mapLayout.mapW, mapLayout.mapH]);

    return activeRegions.map((region, ri) => {
      // Gather cells and edges for this region
      const cellEdges = [];
      const cellPolys = [];
      for (const nid of region.node_ids || []) {
        const idx = nodeIndex[nid];
        if (idx === undefined) continue;
        const cell = voronoi.cellPolygon(idx);
        if (!cell || cell.length < 3) continue;

        const scaledVertices = cell.map(([x, y]) => [sx(x), sy(y)]);

        // Build fill path
        const pts = scaledVertices.map(([cx, cy]) => `${cx},${cy}`).join(' ');
        cellPolys.push(`M${pts}Z`);

        // Collect edges for boundary detection
        for (let j = 0; j < scaledVertices.length; j++) {
          const a = scaledVertices[j];
          const b = scaledVertices[(j + 1) % scaledVertices.length];
          // Normalize edge key: sort points so a->b and b->a produce the same key
          const key = a[0] < b[0] || (a[0] === b[0] && a[1] < b[1])
            ? `${a[0]},${a[1]}|${b[0]},${b[1]}`
            : `${b[0]},${b[1]}|${a[0]},${a[1]}`;
          cellEdges.push(key);
        }
      }
      if (!cellPolys.length) return null;

      // Count edge occurrences — edges appearing once are outer boundary
      const edgeCount = {};
      for (const key of cellEdges) {
        edgeCount[key] = (edgeCount[key] || 0) + 1;
      }
      const boundaryEdges = new Set();
      for (const [key, count] of Object.entries(edgeCount)) {
        if (count === 1) boundaryEdges.add(key);
      }

      // Trace boundary edges into closed polygon paths
      const boundaryPaths = [];
      const remaining = new Set(boundaryEdges);
      while (remaining.size > 0) {
        let [startKey] = remaining;
        const [startPt, endPt] = startKey.split('|');
        remaining.delete(startKey);

        const pathPoints = [startPt];
        let current = endPt;

        let foundNext = true;
        while (foundNext && current !== startPt) {
          foundNext = false;
          for (const key of remaining) {
            const [a, b] = key.split('|');
            if (a === current || b === current) {
              remaining.delete(key);
              current = a === current ? b : a;
              pathPoints.push(current);
              foundNext = true;
              break;
            }
          }
          if (pathPoints.length > 100) break; // safety
        }

        if (pathPoints.length >= 3) {
          boundaryPaths.push(`M${pathPoints.join(' L')}Z`);
        }
      }

      return {
        region,
        ri,
        fillD: cellPolys.join(' '),
        strokeD: boundaryPaths.length > 0 ? boundaryPaths.join(' ') : null,
      };
    }).filter(Boolean);
  }, [activeNodes, activeRegions, mapLayout, nodeIndex, sx, sy]);

  // v2 connection views keyed by endpoint node id.
  const connViewsByNode = useMemo(
    () => connectionsByNode(normalized?.connections),
    [normalized],
  );

  // Legacy map_connections passed directly as the `connections` prop (only
  // entries in the old flat shape) — fallback for nodes that still carry
  // interlayer_connection_id when no v2 connections exist.
  const legacyConnById = useMemo(() => {
    const map = {};
    (connections || []).forEach((c) => {
      if (c && c.from_node_id) map[c.id] = c;
    });
    return map;
  }, [connections]);

  const isConnectionNode = useCallback((node) =>
    !!connViewsByNode[node.id]?.length
    || INTERLAYER_TYPES.has(node.type)
    || !!node.interlayer_connection_id,
  [connViewsByNode]);

  // Cross-map affordance for a node: prefer the v2 connections array; fall
  // back to the legacy interlayer_connection_id lookup.
  const getNodeConnection = useCallback((node) => {
    const views = connViewsByNode[node.id];
    if (views && views.length) {
      const v = views[0];
      return {
        connection: v.connection,
        pairedNodeId: v.far.node_id,
        targetMapId: v.far.map_id,
        kind: v.connection.kind,
        name: v.connection.name,
      };
    }
    if (node.interlayer_connection_id) {
      const lc = legacyConnById[node.interlayer_connection_id];
      if (lc) {
        const pairedId = node.id === lc.from_node_id ? lc.to_node_id : lc.from_node_id;
        const targetMapId = node.id === lc.from_node_id ? lc.to_layer_id : lc.from_layer_id;
        return {
          connection: lc,
          pairedNodeId: pairedId,
          targetMapId,
          kind: lc.connection_type,
          name: lc.name,
        };
      }
    }
    return null;
  }, [connViewsByNode, legacyConnById]);

  // Map switching: new-style onMapChange(mapId, nodeId?) wins; the old
  // navigateToLayer / onLayerChange props keep working as aliases.
  const canNavigate = !!(onMapChange || navigateToLayer || onLayerChange);
  const changeMap = useCallback((mapId, nodeId) => {
    if (onMapChange) {
      onMapChange(mapId, nodeId);
      return;
    }
    if (nodeId != null && navigateToLayer) {
      navigateToLayer(mapId, nodeId);
      return;
    }
    onLayerChange?.(mapId);
  }, [onMapChange, navigateToLayer, onLayerChange]);

  // Child hierarchies: maps anchored to a node of the active map. Nodes with
  // one get an enter badge; double-clicking the node also descends.
  const childAnchors = useMemo(() => childrenByAnchor(normalized?.mapsById), [normalized]);
  const childMapForNode = useCallback((nodeId) => {
    if (!hasMaps || !activeMap) return null;
    const ids = childAnchors[`${activeMap.map_id}:${nodeId}`];
    return ids && ids.length ? normalized.mapsById[ids[0]] : null;
  }, [childAnchors, hasMaps, activeMap, normalized]);

  const parentMap = hasMaps && activeMap?.parent_map_id
    ? normalized.mapsById[activeMap.parent_map_id]
    : null;

  // Tab strip: the current map's "family" — root + parallel siblings (maps
  // not anchored to a node), plus the active map when it's a child.
  const tabMaps = useMemo(() => {
    if (!normalized) return [];
    const tops = Object.values(normalized.mapsById).filter((m) => m.anchor_node_id == null);
    const root = normalized.mapsById[normalized.rootMapId];
    if (root && !tops.some((m) => m.map_id === root.map_id)) tops.unshift(root);
    if (activeMap && !tops.some((m) => m.map_id === activeMap.map_id)) tops.push(activeMap);
    return tops;
  }, [normalized, activeMap]);

  const crumbs = useMemo(
    () => (normalized && activeMap ? breadcrumb(normalized.mapsById, activeMap.map_id) : []),
    [normalized, activeMap],
  );

  // Default (fully zoomed-out) viewBox: the smallest rect that contains the
  // whole 800x500 content frame while matching the container's aspect ratio,
  // centered on the content. Matching aspect ratios means the SVG never
  // letterboxes, so screen-pixel <-> viewBox math in the gesture handlers is
  // exact for any container shape.
  const defaultVB = useMemo(() => {
    const { viewW, viewH } = mapLayout;
    const contentAspect = viewW / viewH;
    const aspect = containerSize.w > 0 && containerSize.h > 0
      ? containerSize.w / containerSize.h
      : contentAspect;
    let w = viewW;
    let h = viewH;
    if (aspect >= contentAspect) {
      w = viewH * aspect;
    } else {
      h = viewW / aspect;
    }
    return { x: (viewW - w) / 2, y: (viewH - h) / 2, w, h };
  }, [mapLayout, containerSize]);

  // Zooming in shrinks the viewBox, so the *minimum* width is the deep-zoom
  // limit and the *maximum* is the whole-map default view.
  const clampWidth = useCallback(
    (w) => Math.max(defaultVB.w / MAX_ZOOM, Math.min(defaultVB.w, w)),
    [defaultVB],
  );

  const clampViewBox = useCallback((vb) => {
    const w = clampWidth(vb.w);
    const h = w * (defaultVB.h / defaultVB.w);
    const x = Math.max(defaultVB.x, Math.min(defaultVB.x + defaultVB.w - w, vb.x));
    const y = Math.max(defaultVB.y, Math.min(defaultVB.y + defaultVB.h - h, vb.y));
    return { x, y, w, h };
  }, [defaultVB, clampWidth]);

  // Keep the view legal when the container is resized (rotation, fullscreen
  // toggle, panels opening): re-fit around the same center point so whatever
  // the user was looking at stays in the middle. At full zoom-out this snaps
  // exactly to the new default view.
  useEffect(() => {
    setViewBox((prev) => {
      const cx = prev.x + prev.w / 2;
      const cy = prev.y + prev.h / 2;
      const w = clampWidth(prev.w);
      const h = w * (defaultVB.h / defaultVB.w);
      return clampViewBox({ x: cx - w / 2, y: cy - h / 2, w, h });
    });
  }, [clampViewBox, clampWidth, defaultVB]);

  // Reset the view only when the actual displayed map changes (a different
  // layer, or the node set size changes e.g. on first load) — not on every
  // re-render that produces a fresh `activeNodes` array reference with the
  // same content (e.g. periodic game-state polling), which previously caused
  // the map to silently recenter/zoom-out every few seconds while playing.
  // This must run (and be declared) before the focus-node effect below so a
  // simultaneous layer-change + focus-node jump doesn't get clobbered back
  // to the default view after the zoom is applied.
  const mapIdentityKey = `${activeMap?.map_id || 'single'}:${activeNodes.length}`;
  const prevMapIdentityRef = useRef(null);
  // Once-per-node guard for the focus effect below. Cleared on map change:
  // after navigating away and back, re-focusing the same node id is a real
  // request again (e.g. the up-button landing on the same anchor node).
  const focusAppliedRef = useRef(null);
  useEffect(() => {
    if (prevMapIdentityRef.current !== mapIdentityKey) {
      prevMapIdentityRef.current = mapIdentityKey;
      focusAppliedRef.current = null;
      // Keep the current view only when a pending focus targets a node on the
      // newly displayed map (traveling through a connection); otherwise the
      // old map's viewBox is meaningless here — reset to the default view.
      const focusTargetHere = !!focusNodeId && activeNodes.some((n) => n.id === focusNodeId);
      if (!focusTargetHere) {
        setViewBox(defaultVB);
        setSelectedNode(null);
      }
    }
  }, [mapIdentityKey, defaultVB, focusNodeId, activeNodes]);

  // Center + zoom in on a specific node when focusNodeId changes (e.g. after
  // traveling through a layer connection point — land zoomed in on the paired
  // node on the destination layer rather than the default full-map view).
  // Applied exactly once per focus node, and only after the container has been
  // measured (so the zoom is computed against the real on-screen frame, not
  // the unmeasured fallback). Re-running on anything else is actively harmful:
  // game-state polling produces fresh `activeNodes` references, and the
  // container height wobbles a few px when hover rows toggle in the info bar —
  // both used to snap the view back to the focus zoom mid-gesture.
  useEffect(() => {
    if (!focusNodeId) {
      focusAppliedRef.current = null;
      return;
    }
    if (!activeNodes.length || containerSize.w <= 0) return;
    if (focusAppliedRef.current === focusNodeId) return;
    const target = activeNodes.find((n) => n.id === focusNodeId);
    if (target) {
      focusAppliedRef.current = focusNodeId;
      const cx = sx(target.x);
      const cy = sy(target.y);
      const halfW = defaultVB.w / 6;
      const halfH = defaultVB.h / 6;
      setViewBox(clampViewBox({ x: cx - halfW, y: cy - halfH, w: halfW * 2, h: halfH * 2 }));
      setSelectedNode(target);
    }
  }, [focusNodeId, activeNodes, containerSize, defaultVB, clampViewBox, sx, sy]);

  // Fog of war
  const fogEnabled = !!(fogOfWar && activeNodes.length);
  const effectiveRevealedIds = useMemo(() => {
    if (!fogEnabled) return new Set();

    if (fogOfWar.mode === 'radius') {
      const playerId = fogOfWar.playerNodeId;
      const radius = fogOfWar.radiusSteps || 1;
      if (!playerId) return new Set();
      // The radius only means anything on the map the player is standing on;
      // for any other map (e.g. browsing a child hierarchy) fall back to the
      // explicitly revealed set instead of hiding everything.
      if (!activeNodes.some((n) => n.id === playerId)) {
        return new Set(fogOfWar.revealedNodeIds || []);
      }

      const adj = {};
      (activeEdges || []).forEach((e) => {
        if (e.from && e.to) {
          if (!adj[e.from]) adj[e.from] = [];
          if (!adj[e.to]) adj[e.to] = [];
          adj[e.from].push(e.to);
          adj[e.to].push(e.from);
        }
      });

      const visited = new Set([playerId]);
      let frontier = [playerId];
      for (let step = 0; step < radius && frontier.length > 0; step++) {
        const next = [];
        for (const nid of frontier) {
          for (const nb of (adj[nid] || [])) {
            if (!visited.has(nb)) {
              visited.add(nb);
              next.push(nb);
            }
          }
        }
        frontier = next;
      }
      return visited;
    }

    // manual mode
    return new Set(fogOfWar.revealedNodeIds || []);
  }, [fogEnabled, fogOfWar, activeEdges, activeNodes]);

  const isNodeRevealed = useCallback((nodeId) => {
    if (!fogEnabled) return true;
    return effectiveRevealedIds.has(nodeId);
  }, [fogEnabled, effectiveRevealedIds]);

  const isEdgeRevealed = useCallback((fromId, toId) => {
    if (!fogEnabled) return true;
    return effectiveRevealedIds.has(fromId) || effectiveRevealedIds.has(toId);
  }, [fogEnabled, effectiveRevealedIds]);

  // Fog mask overlay: dark rect with cutouts for revealed nodes
  const fogMaskId = 'fog-mask';
  const fogCutoutRadius = fogEnabled ? 28 : 0;

  // Zoom/pan handlers
  const handleWheel = useCallback((e) => {
    e.preventDefault();
    const rect = mapContainerRef.current?.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    setViewBox((prev) => {
      const scaleX = prev.w / rect.width;
      const scaleY = prev.h / rect.height;
      const worldX = prev.x + mx * scaleX;
      const worldY = prev.y + my * scaleY;

      // Clamp the width *before* deriving the position: computing x/y from an
      // out-of-range width and clamping afterwards translates the view on
      // every zoom attempt at the limit instead of doing nothing.
      const factor = e.deltaY < 0 ? 0.85 : 1.15;
      const newW = clampWidth(prev.w * factor);
      const newH = newW * (defaultVB.h / defaultVB.w);
      const newX = worldX - mx * (newW / rect.width);
      const newY = worldY - my * (newH / rect.height);

      return clampViewBox({ x: newX, y: newY, w: newW, h: newH });
    });
  }, [clampViewBox, clampWidth, defaultVB]);

  // Touch pan (one finger) + pinch-zoom (two fingers). Mirrors the mouse-drag
  // and wheel-zoom math but reads the live view from the setViewBox updater's
  // `prev`, so these can be attached as stable native listeners. We only
  // preventDefault on move (not start), so a stationary tap still fires the
  // node's onClick for selection while a drag/pinch is captured for navigation.
  const handleTouchStart = useCallback((e) => {
    const rect = mapContainerRef.current?.getBoundingClientRect();
    if (!rect) return;
    if (e.touches.length === 1) {
      const t = e.touches[0];
      touchStateRef.current = { mode: 'pan', lastX: t.clientX, lastY: t.clientY };
    } else if (e.touches.length === 2) {
      const [a, b] = [e.touches[0], e.touches[1]];
      touchStateRef.current = {
        mode: 'pinch',
        lastDist: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY),
      };
    }
  }, []);

  const handleTouchMove = useCallback((e) => {
    const st = touchStateRef.current;
    const rect = mapContainerRef.current?.getBoundingClientRect();
    if (!st || !rect) return;
    e.preventDefault();

    if (st.mode === 'pan' && e.touches.length === 1) {
      const t = e.touches[0];
      const pxDX = st.lastX - t.clientX;
      const pxDY = st.lastY - t.clientY;
      st.lastX = t.clientX;
      st.lastY = t.clientY;
      setViewBox((prev) => clampViewBox({
        ...prev,
        x: prev.x + pxDX * (prev.w / rect.width),
        y: prev.y + pxDY * (prev.h / rect.height),
      }));
    } else if (st.mode === 'pinch' && e.touches.length === 2) {
      const [a, b] = [e.touches[0], e.touches[1]];
      const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      const factor = dist > 0 ? st.lastDist / dist : 1;
      st.lastDist = dist;
      const mx = (a.clientX + b.clientX) / 2 - rect.left;
      const my = (a.clientY + b.clientY) / 2 - rect.top;
      setViewBox((prev) => {
        const worldX = prev.x + mx * (prev.w / rect.width);
        const worldY = prev.y + my * (prev.h / rect.height);
        // Width clamped first — see handleWheel for why.
        const newW = clampWidth(prev.w * factor);
        const newH = newW * (defaultVB.h / defaultVB.w);
        const newX = worldX - mx * (newW / rect.width);
        const newY = worldY - my * (newH / rect.height);
        return clampViewBox({ x: newX, y: newY, w: newW, h: newH });
      });
    }
  }, [clampViewBox, clampWidth, defaultVB]);

  const handleTouchEnd = useCallback((e) => {
    // Fingers lifted: end the gesture, or fall back to panning with the finger
    // that's still down after a pinch releases one.
    if (e.touches.length === 0) {
      touchStateRef.current = null;
    } else if (e.touches.length === 1) {
      const t = e.touches[0];
      touchStateRef.current = { mode: 'pan', lastX: t.clientX, lastY: t.clientY };
    }
  }, []);

  const handleMouseDown = useCallback((e) => {
    if (e.button !== 0) return;
    setDragging(true);
    setDragStart({ x: e.clientX, y: e.clientY });
    e.currentTarget.style.cursor = 'grabbing';
  }, []);

  const handleMouseMove = useCallback((e) => {
    if (!dragging) return;
    const svgEl = e.currentTarget;
    const rect = svgEl.getBoundingClientRect();
    const dx = (dragStart.x - e.clientX) * (viewBox.w / rect.width);
    const dy = (dragStart.y - e.clientY) * (viewBox.h / rect.height);
    setDragStart({ x: e.clientX, y: e.clientY });
    setViewBox((prev) => clampViewBox({ ...prev, x: prev.x + dx, y: prev.y + dy }));
  }, [dragging, dragStart, viewBox.w, viewBox.h, clampViewBox]);

  const handleMouseUp = useCallback((e) => {
    setDragging(false);
    e.currentTarget.style.cursor = '';
  }, []);

  const handleDoubleClick = useCallback(() => {
    if (hoveredNode && canNavigate) {
      const conn = getNodeConnection(hoveredNode);
      if (conn && conn.targetMapId) {
        if (fogEnabled && !isNodeRevealed(conn.pairedNodeId)) {
          // Don't navigate if paired node is unrevealed
          return;
        }
        changeMap(conn.targetMapId, conn.pairedNodeId);
        return;
      }
      // No connection: descend into the node's child hierarchy if it has one.
      const child = childMapForNode(hoveredNode.id);
      if (child && !(fogEnabled && !isNodeRevealed(hoveredNode.id))) {
        changeMap(child.map_id);
        return;
      }
    }
    setViewBox(defaultVB);
  }, [defaultVB, hoveredNode, canNavigate, changeMap, getNodeConnection, childMapForNode, fogEnabled, isNodeRevealed]);

  // Center the view back on the player: zoom to their marker on the current
  // map, or — when a different map is being browsed — jump to the player's
  // map focused on their node.
  const playerNodeId = fogOfWar?.playerNodeId || null;
  // Legacy saves carry only the node id, not the map id; node ids are globally
  // unique, so the player's map can be recovered by searching every map.
  const playerHomeMapId = useMemo(() => {
    if (playerMapId) return playerMapId;
    if (!hasMaps || !playerNodeId) return null;
    const rec = Object.values(normalized.mapsById)
      .find((m) => (m.nodes || []).some((n) => n.id === playerNodeId));
    return rec ? rec.map_id : null;
  }, [playerMapId, hasMaps, normalized, playerNodeId]);
  const centerOnPlayer = useCallback(() => {
    if (playerMarker) {
      const cx = sx(playerMarker.x);
      const cy = sy(playerMarker.y);
      const halfW = defaultVB.w / 6;
      const halfH = defaultVB.h / 6;
      setViewBox(clampViewBox({ x: cx - halfW, y: cy - halfH, w: halfW * 2, h: halfH * 2 }));
      return;
    }
    if (playerNodeId && playerHomeMapId && canNavigate && hasMaps
        && activeMap && playerHomeMapId !== activeMap.map_id) {
      changeMap(playerHomeMapId, playerNodeId);
    }
  }, [playerMarker, playerNodeId, playerHomeMapId, canNavigate, hasMaps, activeMap,
    changeMap, defaultVB, clampViewBox, sx, sy]);
  const canCenterOnPlayer = !!(playerMarker
    || (playerNodeId && playerHomeMapId && canNavigate && hasMaps && activeMap
      && playerHomeMapId !== activeMap.map_id && normalized?.mapsById?.[playerHomeMapId]));

  const handleZoomButton = useCallback((dir) => {
    setViewBox((prev) => {
      const factor = dir > 0 ? 0.75 : 1.333;
      const cx = prev.x + prev.w / 2;
      const cy = prev.y + prev.h / 2;
      const newW = clampWidth(prev.w * factor);
      const newH = newW * (defaultVB.h / defaultVB.w);
      return clampViewBox({
        x: cx - newW / 2,
        y: cy - newH / 2,
        w: newW,
        h: newH,
      });
    });
  }, [clampViewBox, clampWidth, defaultVB]);

  const toggleFullscreen = useCallback(() => {
    const el = wrapperRef.current;
    if (!el) return;
    if (!document.fullscreenElement) {
      el.requestFullscreen?.();
    } else {
      document.exitFullscreen?.();
    }
  }, []);

  // Track fullscreen state (covers Esc-to-exit and OS-level changes).
  useEffect(() => {
    const onChange = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  // Keep on-screen node size constant as the user zooms and across container
  // sizes: a length of L*nodeScale viewBox units renders as L*UI_SCALE CSS
  // pixels regardless of zoom level or how big the map is on screen (the old
  // divisor was the default viewBox width, which made markers and labels
  // shrink with the container — near-unreadable in the mobile popup).
  const nodeScale = (viewBox.w / (containerSize.w || mapLayout.viewW)) * UI_SCALE;

  // Attach wheel listener with passive:false so preventDefault blocks page scroll
  useEffect(() => {
    const el = mapContainerRef.current;
    if (!el) return;
    el.addEventListener('wheel', handleWheel, { passive: false });
    return () => el.removeEventListener('wheel', handleWheel);
  }, [handleWheel]);

  // Track the container's on-screen size. The container only mounts once there
  // are nodes to show (see the empty-state return below), hence the hasNodes
  // dependency to re-attach the observer when data arrives.
  const hasNodes = !!(activeNodes && activeNodes.length);
  useEffect(() => {
    const el = mapContainerRef.current;
    if (!el || !hasNodes) return undefined;
    const update = () => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        setContainerSize((prev) => (
          prev.w === r.width && prev.h === r.height ? prev : { w: r.width, h: r.height }
        ));
      }
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [hasNodes]);

  // Touch listeners must be native + non-passive so touchmove can preventDefault
  // (React attaches these as passive, where preventDefault is a no-op).
  useEffect(() => {
    const el = mapContainerRef.current;
    if (!el) return;
    el.addEventListener('touchstart', handleTouchStart, { passive: false });
    el.addEventListener('touchmove', handleTouchMove, { passive: false });
    el.addEventListener('touchend', handleTouchEnd, { passive: false });
    el.addEventListener('touchcancel', handleTouchEnd, { passive: false });
    return () => {
      el.removeEventListener('touchstart', handleTouchStart);
      el.removeEventListener('touchmove', handleTouchMove);
      el.removeEventListener('touchend', handleTouchEnd);
      el.removeEventListener('touchcancel', handleTouchEnd);
    };
  }, [handleTouchStart, handleTouchMove, handleTouchEnd]);

  if (!activeNodes || !activeNodes.length) {
    return (
      <div className="text-gray-500 text-sm text-center py-8 border border-gray-700 rounded-lg bg-gray-900/50">
        No map data available
      </div>
    );
  }

  return (
    <div
      ref={wrapperRef}
      className={`border border-gray-700 bg-gray-900/50 overflow-hidden flex flex-col ${isFullscreen ? 'w-screen h-screen rounded-none' : 'w-full h-full rounded-lg'}`}
    >
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-700 bg-gray-900/80">
        <span className="text-xs text-gray-400">
          {activeNodes.length} nodes &middot; {activeRoads?.length || 0} routes &middot; {activeRegions?.length || 0} regions
        </span>
        <div className="flex items-center gap-2">
          {canCenterOnPlayer && (
            <button
              onClick={centerOnPlayer}
              className="text-amber-400 hover:text-amber-300 text-sm px-1"
              title="Center on your location"
            >
              {'\u2316'}
            </button>
          )}
          {parentMap && canNavigate && (
            <button
              onClick={() => changeMap(parentMap.map_id, activeMap.anchor_node_id || undefined)}
              className="text-purple-400 hover:text-purple-300 text-sm px-1"
              title={`Up to ${parentMap.label || parentMap.map_id}`}
            >
              {'\u2191'}
            </button>
          )}
          <button
            onClick={() => handleZoomButton(-1)}
            className="text-gray-400 hover:text-gray-200 text-sm px-1"
          >
            {'\u2212'}
          </button>
          <span className="text-xs text-gray-500">
            {Math.round((defaultVB.w / Math.max(0.1, viewBox.w)) * 100)}%
          </span>
          <button
            onClick={() => handleZoomButton(1)}
            className="text-gray-400 hover:text-gray-200 text-sm px-1"
          >
            +
          </button>
          <button
            onClick={handleDoubleClick}
            className="text-gray-500 hover:text-gray-300 text-[10px] px-1 ml-1"
            title="Reset view"
          >
            {'\u21BA'}
          </button>
          <button
            onClick={toggleFullscreen}
            className="text-gray-500 hover:text-gray-300 text-xs px-1"
            title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
          >
            {isFullscreen ? '\u2715' : '\u26F6'}
          </button>
        </div>
      </div>

      {hasMaps && crumbs.length > 1 && (
        <div className="flex items-center gap-1 px-3 py-1 border-b border-gray-700 bg-gray-900/70 text-[10px] text-gray-500 whitespace-nowrap overflow-x-auto">
          {crumbs.map((m, i) => (
            <span key={m.map_id} className="flex items-center gap-1">
              {i > 0 && <span className="text-gray-600">{'\u203A'}</span>}
              {m.map_id === activeMap?.map_id ? (
                <span className="text-purple-300">{m.label || m.map_id}</span>
              ) : (
                <button
                  onClick={() => changeMap(m.map_id)}
                  className="hover:text-gray-300 underline decoration-dotted"
                >
                  {m.label || m.map_id}
                </button>
              )}
            </span>
          ))}
        </div>
      )}

      {hasMaps && (
        <div className="flex gap-1 px-3 py-2 border-b border-gray-700 bg-gray-850 overflow-x-auto">
          {tabMaps.map((m) => {
            const isActive = m.map_id === activeMap?.map_id;
            const icon = LAYER_ICONS[m.level_type] || '\u25CB';
            return (
              <button
                key={m.map_id}
                onClick={() => changeMap(m.map_id)}
                className={`flex items-center gap-1.5 px-3 py-1 rounded text-xs whitespace-nowrap transition-colors ${
                  isActive
                    ? 'bg-purple-600/40 text-purple-200 border border-purple-500/50'
                    : 'bg-gray-800 text-gray-400 border border-gray-700 hover:border-gray-600'
                }`}
              >
                <span className="text-sm">{icon}</span>
                {m.label || m.map_id}
                {m.description && (
                  <span className="text-gray-500 ml-1 hidden sm:inline truncate max-w-[120px]" title={m.description}>
                    &mdash; {m.description.slice(0, 30)}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      <div
        ref={mapContainerRef}
        className="relative flex-1 min-h-0 w-full"
        style={{ overflow: 'hidden', touchAction: 'none' }}
      >
        <svg
          viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
          width="100%"
          height="100%"
          preserveAspectRatio="xMidYMid meet"
          className="block select-none"
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
          onDoubleClick={handleDoubleClick}
          style={{ cursor: dragging ? 'grabbing' : 'grab' }}
        >
          <defs>
            <radialGradient id="settlement-glow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="rgba(245,158,11,0.3)" />
              <stop offset="100%" stopColor="rgba(245,158,11,0)" />
            </radialGradient>
            <radialGradient id="landmark-glow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="rgba(59,130,246,0.3)" />
              <stop offset="100%" stopColor="rgba(59,130,246,0)" />
            </radialGradient>
            <radialGradient id="connection-glow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="rgba(139,92,246,0.35)" />
              <stop offset="100%" stopColor="rgba(139,92,246,0)" />
            </radialGradient>
            <filter id="hover-glow">
              <feGaussianBlur stdDeviation="2" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
            {fogEnabled && (
              <mask id={fogMaskId}>
                <rect x={0} y={0} width={mapLayout.mapW} height={mapLayout.mapH} fill="white" />
                {activeNodes.map((n) => {
                  if (!effectiveRevealedIds.has(n.id)) return null;
                  return (
                    <circle
                      key={`mask-${n.id}`}
                      cx={sx(n.x)}
                      cy={sy(n.y)}
                      r={fogCutoutRadius}
                      fill="black"
                    />
                  );
                })}
              </mask>
            )}
          </defs>

          {/* Terrain background (biome raster) for the active layer — surface
              terrain or the underground cave render, keyed by layer id */}
          {terrainImageUrl && (
            <image
              href={terrainImageUrl}
              x={sx(0)}
              y={sy(0)}
              width={mapLayout.mapW * mapLayout.scale}
              height={mapLayout.mapH * mapLayout.scale}
              preserveAspectRatio="none"
              opacity={0.85}
              pointerEvents="none"
            />
          )}

          {/* Voronoi region polygons */}
          {voronoiRegions.map((vr) => {
            const ri = vr.ri;
            const isHoveredRegion = hoveredRegion === vr.region.region_name;
            const hoverHandlers = {
              onMouseEnter: () => setHoveredRegion(vr.region.region_name),
              onMouseLeave: () => setHoveredRegion(null),
              style: { cursor: 'pointer' },
            };
            return (
              <g key={`vreg-${ri}`}>
                {/* Fully transparent fill — keeps the region hoverable while
                    letting the terrain/world map show through. The outline
                    below is what visually delineates the region. */}
                <path
                  d={vr.fillD}
                  fill="transparent"
                  stroke="none"
                  {...hoverHandlers}
                />
                {/* Stroke: only outer boundary */}
                {vr.strokeD && (
                  <path
                    d={vr.strokeD}
                    fill="none"
                    stroke={isHoveredRegion
                      ? REGION_STROKE_COLORS[ri % REGION_STROKE_COLORS.length].replace('0.45', '0.9')
                      : REGION_STROKE_COLORS[ri % REGION_STROKE_COLORS.length]}
                    strokeWidth={isHoveredRegion ? 1.5 : 0.6}
                    pointerEvents="none"
                  />
                )}
              </g>
            );
          })}

          {/* Minor paths: spurs from each hub to other locations. Render under
              the main roads; fade thinner/lighter the lower the node's
              importance. */}
          {activeRoads && activeRoads.map((road, i) => {
            if (road.tier !== 'path') return null;
            if (!road.path || road.path.length < 2) return null;
            if (!isEdgeRevealed(road.from, road.to)) return null;
            const imp = road.importance ?? 0;
            const pts = road.path.map(([x, y]) => `${sx(x)},${sy(y)}`).join(' ');
            return (
              <polyline
                key={`path-${i}`}
                points={pts}
                fill="none"
                stroke={`rgba(120,72,30,${(0.15 + imp * 0.05).toFixed(3)})`}
                strokeWidth={0.4 + imp * 0.09}
                strokeLinejoin="round"
                strokeLinecap="round"
                strokeDasharray="2 3"
                pointerEvents="none"
              />
            );
          })}

          {/* City street fabric (city_roadnet): lanes under side streets
              under avenues, solid grey so the network reads as pavement,
              not trails. */}
          {activeRoads && activeRoads.map((road, i) => {
            if (road.tier !== 'lane') return null;
            if (!road.path || road.path.length < 2) return null;
            if (!isEdgeRevealed(road.from, road.to)) return null;
            const pts = road.path.map(([x, y]) => `${sx(x)},${sy(y)}`).join(' ');
            return (
              <polyline
                key={`lane-${i}`}
                points={pts}
                fill="none"
                stroke="rgba(148,163,184,0.18)"
                strokeWidth={0.45}
                strokeLinejoin="round"
                strokeLinecap="round"
                pointerEvents="none"
              />
            );
          })}
          {activeRoads && activeRoads.map((road, i) => {
            if (road.tier !== 'street') return null;
            if (!road.path || road.path.length < 2) return null;
            if (!isEdgeRevealed(road.from, road.to)) return null;
            const pts = road.path.map(([x, y]) => `${sx(x)},${sy(y)}`).join(' ');
            return (
              <polyline
                key={`street-${i}`}
                points={pts}
                fill="none"
                stroke="rgba(148,163,184,0.30)"
                strokeWidth={0.7}
                strokeLinejoin="round"
                strokeLinecap="round"
                pointerEvents="none"
              />
            );
          })}
          {activeRoads && activeRoads.map((road, i) => {
            if (road.tier !== 'avenue') return null;
            if (!road.path || road.path.length < 2) return null;
            if (!isEdgeRevealed(road.from, road.to)) return null;
            const pts = road.path.map(([x, y]) => `${sx(x)},${sy(y)}`).join(' ');
            return (
              <polyline
                key={`avenue-${i}`}
                points={pts}
                fill="none"
                stroke="rgba(148,163,184,0.55)"
                strokeWidth={1.4}
                strokeLinejoin="round"
                strokeLinecap="round"
                pointerEvents="none"
              />
            );
          })}

          {/* Roads: terrain-following least-cost paths between settlements */}
          {activeRoads && activeRoads.map((road, i) => {
            if (['path', 'street', 'avenue', 'lane'].includes(road.tier)) return null;
            if (!road.path || road.path.length < 2) return null;
            if (!isEdgeRevealed(road.from, road.to)) return null;
            const pts = road.path.map(([x, y]) => `${sx(x)},${sy(y)}`).join(' ');
            return (
              <polyline
                key={`road-${i}`}
                points={pts}
                fill="none"
                stroke="rgba(120,72,30,0.85)"
                strokeWidth={1.6}
                strokeLinejoin="round"
                strokeLinecap="round"
                strokeDasharray="4 2"
                pointerEvents="none"
              />
            );
          })}

          {/* Nodes */}
          {activeNodes.map((node) => {
            const r = getImportanceRadius(node.importance, nodeScale);
            const color = TYPE_COLORS[node.type] || '#6b7280';
            const isHovered = hoveredNode?.id === node.id;
            const isPopulated = !!node.name;
            const isConn = isConnectionNode(node);
            const nodeConn = isConn ? getNodeConnection(node) : null;
            const revealed = isNodeRevealed(node.id);

            return (
              <g
                key={node.id}
                onMouseEnter={() => { if (revealed) setHoveredNode(node); }}
                onMouseLeave={() => setHoveredNode(null)}
                onClick={() => {
                  if (!revealed) return;
                  // Clicking a connection glyph travels to the far map.
                  if (nodeConn && nodeConn.targetMapId && nodeConn.targetMapId !== activeMap?.map_id
                      && canNavigate && !(fogEnabled && !isNodeRevealed(nodeConn.pairedNodeId))) {
                    changeMap(nodeConn.targetMapId, nodeConn.pairedNodeId);
                    return;
                  }
                  setSelectedNode(node);
                }}
                style={{ cursor: revealed ? 'pointer' : 'default', opacity: revealed ? 1 : 0.08, transition: 'opacity 0.3s' }}
              >
                {nodeConn && revealed && (
                  <title>
                    {`${nodeConn.name || node.name || 'Connection'} (${nodeConn.kind || 'link'})${
                      nodeConn.targetMapId
                        ? ` → ${normalized?.mapsById?.[nodeConn.targetMapId]?.label || nodeConn.targetMapId}`
                        : ''
                    }`}
                  </title>
                )}
                {isPopulated && revealed && (
                  <circle
                    cx={sx(node.x)}
                    cy={sy(node.y)}
                    r={r + 4 * nodeScale}
                    fill={isConn ? 'url(#connection-glow)' : node.type === 'settlement' ? 'url(#settlement-glow)' : 'url(#landmark-glow)'}
                    opacity={isHovered ? 1 : 0.6}
                  />
                )}
                <NodeShape
                  cx={sx(node.x)}
                  cy={sy(node.y)}
                  r={r}
                  color={color}
                  isHovered={isHovered}
                  isPopulated={isPopulated}
                  isConnection={isConn}
                  connectionType={node.type}
                  scale={nodeScale}
                />
                {isPopulated && revealed && (
                  <>
                    <text
                      x={sx(node.x)}
                      y={sy(node.y) + r + 10 * nodeScale}
                      textAnchor="middle"
                      className="fill-amber-300 font-semibold"
                      style={{ fontFamily: 'monospace', fontSize: 8 * nodeScale, pointerEvents: 'none' }}
                    >
                      {node.name.length > 18 ? node.name.slice(0, 17) + '\u2026' : node.name}
                    </text>
                    {node.label_description && (
                      <text
                        x={sx(node.x)}
                        y={sy(node.y) + r + 20 * nodeScale}
                        textAnchor="middle"
                        className="fill-gray-500 italic"
                        style={{ fontFamily: 'monospace', fontSize: 7 * nodeScale, pointerEvents: 'none' }}
                      >
                        {node.label_description.length > 32 ? node.label_description.slice(0, 31) + '\u2026' : node.label_description}
                      </text>
                    )}
                  </>
                )}
              </g>
            );
          })}

          {/* Enter badges for nodes anchoring a child hierarchy — drawn as a
              separate layer above every node so a neighboring node rendered
              later can't cover the tap target. */}
          {canNavigate && activeNodes.map((node) => {
            const child = childMapForNode(node.id);
            if (!child || !isNodeRevealed(node.id)) return null;
            const r = getImportanceRadius(node.importance, nodeScale);
            const bx = sx(node.x) + r + 3 * nodeScale;
            const by = sy(node.y) - r - 3 * nodeScale;
            return (
              <g
                key={`enter-${node.id}`}
                onClick={(e) => {
                  e.stopPropagation();
                  changeMap(child.map_id);
                }}
                style={{ cursor: 'pointer' }}
              >
                <title>{`Enter ${child.label || child.map_id}`}</title>
                <circle
                  cx={bx}
                  cy={by}
                  r={3.4 * nodeScale}
                  fill="rgba(17,24,39,0.95)"
                  stroke="#a78bfa"
                  strokeWidth={0.7 * nodeScale}
                />
                <path
                  d={`M ${bx - 1.6 * nodeScale} ${by} H ${bx + 1.6 * nodeScale} M ${bx} ${by - 1.6 * nodeScale} V ${by + 1.6 * nodeScale}`}
                  stroke="#a78bfa"
                  strokeWidth={0.8 * nodeScale}
                  strokeLinecap="round"
                />
              </g>
            );
          })}

          {/* Fog of war overlay */}
          {fogEnabled && (
            <rect
              x={0}
              y={0}
              width={mapLayout.mapW}
              height={mapLayout.mapH}
              fill="rgba(0,0,0,0.55)"
              mask={`url(#${fogMaskId})`}
              style={{ pointerEvents: 'none' }}
            />
          )}

          {/* Player marker (drawn above the fog so a mid-edge position stays visible) */}
          {playerMarker && (
            <g pointerEvents="none">
              {playerMarker.traveling && (
                <line
                  x1={sx(playerMarker.from.x)}
                  y1={sy(playerMarker.from.y)}
                  x2={sx(playerMarker.to.x)}
                  y2={sy(playerMarker.to.y)}
                  stroke="rgba(251,191,36,0.65)"
                  strokeWidth={1.2 * nodeScale}
                  strokeDasharray="3 2"
                />
              )}
              <circle cx={sx(playerMarker.x)} cy={sy(playerMarker.y)} r={5 * nodeScale} fill="rgba(251,191,36,0.25)">
                <animate
                  attributeName="r"
                  values={`${4 * nodeScale};${8 * nodeScale};${4 * nodeScale}`}
                  dur="2s"
                  repeatCount="indefinite"
                />
              </circle>
              <circle
                cx={sx(playerMarker.x)}
                cy={sy(playerMarker.y)}
                r={2.6 * nodeScale}
                fill="#fbbf24"
                stroke="#fff"
                strokeWidth={0.8 * nodeScale}
              />
            </g>
          )}
        </svg>
      </div>

      {/* Info bar: last-clicked node + region hover */}
      {(selectedNode || hoveredRegion) && (
        <div className="px-3 py-2 border-t border-gray-700 bg-gray-900/90 space-y-1">
          {hoveredRegion && (
            <div className="flex items-center gap-2 text-xs text-gray-300">
              <span
                className="inline-block w-2.5 h-2.5 rounded"
                style={{
                  backgroundColor: REGION_STROKE_COLORS[
                    activeRegions.findIndex((r) => r.region_name === hoveredRegion) % REGION_STROKE_COLORS.length
                  ] || 'rgba(139,92,246,0.7)',
                }}
              />
              <span className="font-medium">{hoveredRegion}</span>
              <span className="text-gray-600">region</span>
              <span className="text-gray-500 ml-auto">
                {(() => {
                  const reg = activeRegions.find((r) => r.region_name === hoveredRegion);
                  return reg ? `${reg.node_ids?.length || 0} nodes` : '';
                })()}
              </span>
            </div>
          )}
          {selectedNode && (
            <>
              {/* Header row doubles as the expand/collapse toggle so the
                  description doesn't crowd out the map in small views. */}
              <button
                type="button"
                onClick={() => setInfoExpandedOverride(!infoExpanded)}
                className="w-full flex items-center gap-2 text-left"
              >
                <span
                  className={`inline-block flex-shrink-0 ${isConnectionNode(selectedNode) ? 'rotate-45 w-2.5 h-2.5' : 'rounded-full w-2.5 h-2.5'}`}
                  style={{ backgroundColor: isConnectionNode(selectedNode) ? (CONNECTION_COLORS[selectedNode.type] || '#8b5cf6') : (TYPE_COLORS[selectedNode.type] || '#6b7280') }}
                />
                <span className="text-sm font-medium text-gray-200">
                  {selectedNode.name || `Waypoint ${selectedNode.id}`}
                </span>
                <span className="text-[10px] uppercase text-gray-500 bg-gray-800 px-1.5 py-0.5 rounded">
                  {selectedNode.type}
                </span>
                {selectedNode.layer_id && (
                  <span className="text-[10px] text-purple-400 bg-purple-900/30 px-1.5 py-0.5 rounded">
                    {selectedNode.layer_id}
                  </span>
                )}
                <span className="text-xs text-purple-400 ml-auto">
                  Importance: {selectedNode.importance}/10
                </span>
                <span className="text-gray-500 text-xs flex-shrink-0">
                  {infoExpanded ? '▾' : '▸'}
                </span>
              </button>
              {infoExpanded && selectedNode.description && (
                <p className="text-xs text-gray-400 leading-relaxed">
                  {renderDescriptionWithLinks(selectedNode.description)}
                </p>
              )}
              {/* Connection link info */}
              {infoExpanded && isConnectionNode(selectedNode) && (() => {
                const conn = getNodeConnection(selectedNode);
                if (!conn) return null;
                const pairedNode = (Array.isArray(activeNodes) ? activeNodes : []).find((n) => n.id === conn.pairedNodeId);
                const pairedName = pairedNode?.name || conn.name || conn.pairedNodeId;
                const targetLabel = normalized?.mapsById?.[conn.targetMapId]?.label || conn.targetMapId;
                const isPairedRevealed = fogEnabled ? isNodeRevealed(conn.pairedNodeId) : true;
                return (
                  <div className="flex items-center gap-2 mt-1 pt-1 border-t border-gray-700/50">
                    <span className="text-[10px] text-purple-400 font-medium">
                      Connects to: {targetLabel}
                    </span>
                    <span className="text-xs text-gray-500">
                      — {pairedName} ({conn.kind})
                    </span>
                    {!isPairedRevealed && (
                      <span className="text-[10px] text-gray-600 italic">unrevealed</span>
                    )}
                    {canNavigate && isPairedRevealed && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          changeMap(conn.targetMapId, conn.pairedNodeId);
                        }}
                        className="text-[9px] text-purple-400 hover:text-purple-300 underline ml-1"
                      >
                        jump to
                      </button>
                    )}
                  </div>
                );
              })()}
            </>
          )}
        </div>
      )}
    </div>
  );
}
