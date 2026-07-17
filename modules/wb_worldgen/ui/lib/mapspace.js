// mapspace.js — shared normalizer for wb_worldgen world map data.
//
// The backend's "world_format 2" replaces the legacy `map` / `map_layers` /
// `map_connections` keys with a flat map space:
//   world_data.maps        object keyed by map_id -> MapRecord
//   world_data.connections flat array of cross-map connection records
//   world_data.root_map_id id of the root map ("root")
// Old API responses (e.g. the map_generation step preview during world
// building) may still carry the legacy shape, so every consumer goes through
// normalizeWorldData() and renders from the normalized form.

/**
 * Normalize any world/map payload (v2 or legacy) to
 * `{ mapsById, rootMapId, connections }`.
 *
 * - v2 (`maps` key): passed through unchanged.
 * - legacy layered (`map_layers`, or step-data `layers` entries carrying a
 *   per-layer `map`): each layer becomes a MapRecord keyed by its layer_id
 *   (lossless — `legacy_layer_id` is preserved), and `map_connections` are
 *   converted to v2 connection records.
 * - legacy flat (`map`): a single "root" MapRecord.
 */
export function normalizeWorldData(worldData) {
  if (!worldData) return { mapsById: {}, rootMapId: null, connections: [] };

  // v2: pass through.
  if (worldData.maps) {
    return {
      mapsById: worldData.maps,
      rootMapId: worldData.root_map_id || 'root',
      connections: worldData.connections || [],
    };
  }

  // Legacy layered: world_data.map_layers, or step-data `layers` whose
  // entries carry a per-layer `map` (same record shape).
  const layers = worldData.map_layers
    || (Array.isArray(worldData.layers) && worldData.layers.some((l) => l?.map)
      ? worldData.layers
      : null);
  if (layers && layers.length) {
    const mapsById = {};
    layers.forEach((layer) => {
      mapsById[layer.layer_id] = {
        map_id: layer.layer_id,
        label: layer.name,
        level_type: layer.layer_type,
        description: layer.description,
        parent_map_id: null,
        anchor_node_id: null,
        legacy_layer_id: layer.layer_id,
        ...(layer.map || {}),
      };
    });
    const rawConns = worldData.map_connections || worldData.connections || [];
    const connections = rawConns.map((c) => {
      // Already v2-shaped (endpoints as {map_id, node_id})? Pass through.
      if (c.from && typeof c.from === 'object') return c;
      return {
        id: c.id,
        from: { map_id: c.from_layer_id, node_id: c.from_node_id },
        to: { map_id: c.to_layer_id, node_id: c.to_node_id },
        kind: c.connection_type,
        name: c.name,
        description: c.description,
        travel: { mode: 'instant' },
        bidirectional: c.bidirectional !== false,
        requirements: c.requirements || [],
        hidden: !!c.hidden,
        origin: c.origin,
      };
    });
    return { mapsById, rootMapId: layers[0].layer_id, connections };
  }

  // Legacy flat map.
  if (worldData.map) {
    return {
      mapsById: {
        root: {
          map_id: 'root',
          label: 'World',
          parent_map_id: null,
          anchor_node_id: null,
          ...worldData.map,
        },
      },
      rootMapId: 'root',
      connections: [],
    };
  }

  return { mapsById: {}, rootMapId: null, connections: [] };
}

/**
 * Index v2 connections by endpoint node id. Returns an object mapping
 * node_id -> array of `{connection, near, far}` views, where `near` is the
 * endpoint at that node and `far` the other end. Bidirectional connections
 * produce a view from each end.
 */
export function connectionsByNode(connections) {
  const byNode = {};
  const add = (nodeId, view) => {
    if (!nodeId) return;
    if (!byNode[nodeId]) byNode[nodeId] = [];
    byNode[nodeId].push(view);
  };
  (connections || []).forEach((connection) => {
    const { from, to } = connection;
    if (!from || !to) return;
    add(from.node_id, { connection, near: from, far: to });
    if (connection.bidirectional !== false) {
      add(to.node_id, { connection, near: to, far: from });
    }
  });
  return byNode;
}

/**
 * Walk parent_map_id links from mapId up to the root and return the chain of
 * MapRecords ordered root -> mapId. Cycle-safe; unknown ids yield [].
 */
export function breadcrumb(mapsById, mapId) {
  const chain = [];
  const seen = new Set();
  let current = mapId;
  while (current && mapsById?.[current] && !seen.has(current)) {
    seen.add(current);
    chain.unshift(mapsById[current]);
    current = mapsById[current].parent_map_id;
  }
  return chain;
}

/**
 * Group child maps by their anchor: `${parentMapId}:${anchorNodeId}` -> [mapId].
 * Maps without a parent or anchor (root / parallel siblings) are excluded.
 */
export function childrenByAnchor(mapsById) {
  const out = {};
  Object.values(mapsById || {}).forEach((m) => {
    if (!m.parent_map_id || !m.anchor_node_id) return;
    const key = `${m.parent_map_id}:${m.anchor_node_id}`;
    if (!out[key]) out[key] = [];
    out[key].push(m.map_id);
  });
  return out;
}
