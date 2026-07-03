import { useState, useMemo, useCallback, useEffect, useRef } from 'react';
import { Delaunay } from 'd3-delaunay';

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

export default function MapRenderer({ nodes, edges, regions, config, layers, connections, activeLayerId, onLayerChange, fogOfWar, navigateToLayer, focusNodeId, worldId, roads }) {
  const [hoveredNode, setHoveredNode] = useState(null);
  const [hoveredRegion, setHoveredRegion] = useState(null);
  const [selectedNode, setSelectedNode] = useState(null);
  const [viewBox, setViewBox] = useState({ x: 0, y: 0, w: 800, h: 500 });
  const [dragging, setDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const [isFullscreen, setIsFullscreen] = useState(false);
  const mapContainerRef = useRef(null);
  const wrapperRef = useRef(null);

  const hasLayers = layers && layers.length > 0;
  const activeLayer = hasLayers
    ? layers.find((l) => l.layer_id === activeLayerId) || layers[0]
    : null;

  const activeNodes = useMemo(() => {
    if (!hasLayers) return nodes || [];
    if (!activeLayer) return [];
    const map = activeLayer.map || {};
    return map.nodes || [];
  }, [hasLayers, activeLayer, nodes]);

  const activeEdges = useMemo(() => {
    if (!hasLayers) return edges || [];
    if (!activeLayer) return [];
    const map = activeLayer.map || {};
    return map.edges || [];
  }, [hasLayers, activeLayer, edges]);

  const activeRegions = useMemo(() => {
    if (!hasLayers) return regions || [];
    if (!activeLayer) return [];
    const map = activeLayer.map || {};
    return map.regions || [];
  }, [hasLayers, activeLayer, regions]);

  const activeRoads = useMemo(() => {
    if (!hasLayers) return roads || [];
    if (!activeLayer) return [];
    return (activeLayer.map || {}).roads || [];
  }, [hasLayers, activeLayer, roads]);

  // Terrain background image. Every layer is its own area with its own raster,
  // so each gets a biome background keyed by layer id.
  const terrainImageUrl = useMemo(() => {
    if (!worldId) return null;
    const layerId = hasLayers ? (activeLayer?.layer_id || 'main') : 'main';
    return `/api/world/${encodeURIComponent(worldId)}/terrain/${encodeURIComponent(layerId)}/biome`;
  }, [worldId, hasLayers, activeLayer]);

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
    const mw = config?.map_width || 1000;
    const mh = config?.map_height || 1000;
    const s = Math.min((viewW - pad * 2) / mw, (viewH - pad * 2) / mh);
    return { pad, viewW, viewH, scale: s, mapW: mw, mapH: mh };
  }, [config]);

  const sx = useCallback((x) => mapLayout.pad + x * mapLayout.scale, [mapLayout]);
  const sy = useCallback((y) => mapLayout.pad + y * mapLayout.scale, [mapLayout]);

  // Node ID -> index in activeNodes
  const nodeIndex = useMemo(() => {
    const idx = {};
    activeNodes.forEach((n, i) => { idx[n.id] = i; });
    return idx;
  }, [activeNodes]);

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

  const isInterlayerNode = (node) =>
    INTERLAYER_TYPES.has(node.type) || !!node.interlayer_connection_id;

  // Connection lookup for inter-layer navigation
  const connectionById = useMemo(() => {
    if (!connections || !connections.length) return {};
    const map = {};
    connections.forEach((c) => { map[c.id] = c; });
    return map;
  }, [connections]);

  const getPairedConnection = useCallback((node) => {
    if (!node.interlayer_connection_id) return null;
    const lc = connectionById[node.interlayer_connection_id];
    if (!lc) return null;
    const pairedId = node.id === lc.from_node_id ? lc.to_node_id : lc.from_node_id;
    const targetLayerId = node.id === lc.from_node_id ? lc.to_layer_id : lc.from_layer_id;
    return { connection: lc, pairedNodeId: pairedId, targetLayerId };
  }, [connectionById]);

  // Reset viewBox when map data changes
  const defaultVB = useMemo(() => ({ x: 0, y: 0, w: mapLayout.viewW, h: mapLayout.viewH }), [mapLayout]);

  const clampViewBox = useCallback((vb) => {
    const minZoom = 0.3;
    const maxZoom = 5;
    const w = Math.max(defaultVB.w * minZoom, Math.min(defaultVB.w * maxZoom, vb.w));
    const h = w * (defaultVB.h / defaultVB.w);
    const x = Math.max(0, Math.min(defaultVB.w - w, vb.x));
    const y = Math.max(0, Math.min(defaultVB.h - h, vb.y));
    return { x, y, w, h };
  }, [defaultVB]);

  // Reset the view only when the actual displayed map changes (a different
  // layer, or the node set size changes e.g. on first load) — not on every
  // re-render that produces a fresh `activeNodes` array reference with the
  // same content (e.g. periodic game-state polling), which previously caused
  // the map to silently recenter/zoom-out every few seconds while playing.
  // This must run (and be declared) before the focus-node effect below so a
  // simultaneous layer-change + focus-node jump doesn't get clobbered back
  // to the default view after the zoom is applied.
  const mapIdentityKey = `${activeLayer?.layer_id || 'single'}:${activeNodes.length}`;
  const prevMapIdentityRef = useRef(null);
  useEffect(() => {
    if (prevMapIdentityRef.current !== mapIdentityKey) {
      prevMapIdentityRef.current = mapIdentityKey;
      if (!focusNodeId) {
        setViewBox(defaultVB);
        setSelectedNode(null);
      }
    }
  }, [mapIdentityKey, defaultVB, focusNodeId]);

  // Center + zoom in on a specific node when focusNodeId changes (e.g. after
  // traveling through a layer connection point — land zoomed in on the paired
  // node on the destination layer rather than the default full-map view).
  useEffect(() => {
    if (!focusNodeId || !activeNodes.length) return;
    const target = activeNodes.find((n) => n.id === focusNodeId);
    if (target) {
      const cx = sx(target.x);
      const cy = sy(target.y);
      const halfW = defaultVB.w / 6;
      const halfH = defaultVB.h / 6;
      setViewBox(clampViewBox({ x: cx - halfW, y: cy - halfH, w: halfW * 2, h: halfH * 2 }));
      setSelectedNode(target);
    }
  }, [focusNodeId, activeNodes, defaultVB, clampViewBox, sx, sy]);

  // Fog of war
  const fogEnabled = !!(fogOfWar && activeNodes.length);
  const effectiveRevealedIds = useMemo(() => {
    if (!fogEnabled) return new Set();

    if (fogOfWar.mode === 'radius') {
      const playerId = fogOfWar.playerNodeId;
      const radius = fogOfWar.radiusSteps || 1;
      if (!playerId) return new Set();

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
  }, [fogEnabled, fogOfWar, activeEdges]);

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

      const factor = e.deltaY < 0 ? 0.85 : 1.15;
      const newW = prev.w * factor;
      const newH = newW * (defaultVB.h / defaultVB.w);
      const newX = worldX - mx * (newW / rect.width);
      const newY = worldY - my * (newH / rect.height);

      return clampViewBox({ x: newX, y: newY, w: newW, h: newH });
    });
  }, [clampViewBox, defaultVB]);

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
    setViewBox((prev) => {
      const newX = prev.x + dx;
      const newY = prev.y + dy;
      return { ...prev, x: Math.max(0, Math.min(defaultVB.w - prev.w, newX)), y: Math.max(0, Math.min(defaultVB.h - prev.h, newY)) };
    });
  }, [dragging, dragStart, viewBox.w, viewBox.h, defaultVB]);

  const handleMouseUp = useCallback((e) => {
    setDragging(false);
    e.currentTarget.style.cursor = '';
  }, []);

  const handleDoubleClick = useCallback(() => {
    if (hoveredNode && isInterlayerNode(hoveredNode) && navigateToLayer) {
      const paired = getPairedConnection(hoveredNode);
      if (paired) {
        if (fogEnabled && !isNodeRevealed(paired.pairedNodeId)) {
          // Don't navigate if paired node is unrevealed
          return;
        }
        navigateToLayer(paired.targetLayerId, paired.pairedNodeId);
        return;
      }
    }
    setViewBox(defaultVB);
  }, [defaultVB, hoveredNode, navigateToLayer, getPairedConnection, fogEnabled, isNodeRevealed]);

  const handleZoomButton = useCallback((dir) => {
    setViewBox((prev) => {
      const factor = dir > 0 ? 0.75 : 1.333;
      const cx = prev.x + prev.w / 2;
      const cy = prev.y + prev.h / 2;
      const newW = prev.w * factor;
      const newH = newW * (defaultVB.h / defaultVB.w);
      return clampViewBox({
        x: cx - newW / 2,
        y: cy - newH / 2,
        w: newW,
        h: newH,
      });
    });
  }, [clampViewBox, defaultVB]);

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

  // Keep on-screen node size constant as the user zooms: the SVG scales the
  // whole viewBox, so divide node radii/strokes by the current zoom factor
  // (viewBox.w / defaultVB.w is 1 at default zoom, <1 when zoomed in).
  const nodeScale = viewBox.w / defaultVB.w;

  // Attach wheel listener with passive:false so preventDefault blocks page scroll
  useEffect(() => {
    const el = mapContainerRef.current;
    if (!el) return;
    el.addEventListener('wheel', handleWheel, { passive: false });
    return () => el.removeEventListener('wheel', handleWheel);
  }, [handleWheel]);

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
      className={`border border-gray-700 bg-gray-900/50 overflow-hidden ${isFullscreen ? 'flex flex-col w-screen h-screen rounded-none' : 'rounded-lg'}`}
    >
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-700 bg-gray-900/80">
        <span className="text-xs text-gray-400">
          {activeNodes.length} nodes &middot; {activeRoads?.length || 0} routes &middot; {activeRegions?.length || 0} regions
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => handleZoomButton(-1)}
            className="text-gray-400 hover:text-gray-200 text-sm px-1"
          >
            \u2212
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
            \u21BA
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

      {hasLayers && (
        <div className="flex gap-1 px-3 py-2 border-b border-gray-700 bg-gray-850 overflow-x-auto">
          {layers.map((layer) => {
            const isActive = layer.layer_id === (activeLayerId || layers[0]?.layer_id);
            const icon = LAYER_ICONS[layer.layer_type] || '\u25CB';
            return (
              <button
                key={layer.layer_id}
                onClick={() => onLayerChange?.(layer.layer_id)}
                className={`flex items-center gap-1.5 px-3 py-1 rounded text-xs whitespace-nowrap transition-colors ${
                  isActive
                    ? 'bg-purple-600/40 text-purple-200 border border-purple-500/50'
                    : 'bg-gray-800 text-gray-400 border border-gray-700 hover:border-gray-600'
                }`}
              >
                <span className="text-sm">{icon}</span>
                {layer.name}
                {layer.description && (
                  <span className="text-gray-500 ml-1 hidden sm:inline truncate max-w-[120px]" title={layer.description}>
                    &mdash; {layer.description.slice(0, 30)}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      <div
        ref={mapContainerRef}
        className={`relative ${isFullscreen ? 'flex-1 min-h-0' : ''}`}
        style={isFullscreen
          ? { width: '100%', overflow: 'hidden' }
          : { width: mapLayout.viewW, height: mapLayout.viewH, overflow: 'hidden' }}
      >
        <svg
          viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
          width={isFullscreen ? '100%' : mapLayout.viewW}
          height={isFullscreen ? '100%' : mapLayout.viewH}
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

          {/* Roads: terrain-following least-cost paths between settlements */}
          {activeRoads && activeRoads.map((road, i) => {
            if (road.tier === 'path') return null;
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
            const isConn = isInterlayerNode(node);
            const revealed = isNodeRevealed(node.id);

            return (
              <g
                key={node.id}
                onMouseEnter={() => { if (revealed) setHoveredNode(node); }}
                onMouseLeave={() => setHoveredNode(null)}
                onClick={() => { if (revealed) setSelectedNode(node); }}
                style={{ cursor: revealed ? 'pointer' : 'default', opacity: revealed ? 1 : 0.08, transition: 'opacity 0.3s' }}
              >
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
              <div className="flex items-center gap-2">
                <span
                  className={`inline-block flex-shrink-0 ${isInterlayerNode(selectedNode) ? 'rotate-45 w-2.5 h-2.5' : 'rounded-full w-2.5 h-2.5'}`}
                  style={{ backgroundColor: isInterlayerNode(selectedNode) ? (CONNECTION_COLORS[selectedNode.type] || '#8b5cf6') : (TYPE_COLORS[selectedNode.type] || '#6b7280') }}
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
              </div>
              {selectedNode.description && (
                <p className="text-xs text-gray-400 leading-relaxed">
                  {renderDescriptionWithLinks(selectedNode.description)}
                </p>
              )}
              {/* Connection link info */}
              {isInterlayerNode(selectedNode) && (() => {
                const paired = getPairedConnection(selectedNode);
                if (!paired) return null;
                const pairedNode = (Array.isArray(activeNodes) ? activeNodes : []).find((n) => n.id === paired.pairedNodeId);
                const pairedName = pairedNode?.name || paired.pairedNodeId;
                const isPairedRevealed = fogEnabled ? isNodeRevealed(paired.pairedNodeId) : true;
                return (
                  <div className="flex items-center gap-2 mt-1 pt-1 border-t border-gray-700/50">
                    <span className="text-[10px] text-purple-400 font-medium">
                      Connects to: {paired.targetLayerId}
                    </span>
                    <span className="text-xs text-gray-500">
                      — {pairedName} ({paired.connection.connection_type})
                    </span>
                    {!isPairedRevealed && (
                      <span className="text-[10px] text-gray-600 italic">unrevealed</span>
                    )}
                    {navigateToLayer && isPairedRevealed && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          navigateToLayer(paired.targetLayerId, paired.pairedNodeId);
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
