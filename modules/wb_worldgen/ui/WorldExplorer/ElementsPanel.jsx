import { useMemo, useState, useEffect } from 'react';
import { TYPE_COLORS, CONNECTION_COLORS, renderDescriptionWithLinks } from '../WorldBuilder/MapRenderer';
import { connectionsByNode } from '../lib/mapspace';

const noteStatusBadge = (note) => {
  if (note?.status === 'amended') {
    return <span className="ml-1 text-[10px] text-amber-400 border border-amber-700/60 rounded px-1">amended</span>;
  }
  if (note?.status === 'no_compromise') {
    return <span className="ml-1 text-[10px] text-red-400 border border-red-800/60 rounded px-1">binding</span>;
  }
  return null;
};

function Section({ title, count, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-b border-gray-800">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-gray-800/40 transition-colors"
      >
        <span className="text-xs font-semibold uppercase tracking-wide text-gray-400">
          {title}
          {count != null && <span className="ml-1.5 text-gray-600 normal-case">({count})</span>}
        </span>
        <span className="text-gray-600 text-xs">{open ? '−' : '+'}</span>
      </button>
      {open && <div className="pb-2">{children}</div>}
    </div>
  );
}

function NodeDetail({ node, views, childMaps, mapsById, onOpenMap }) {
  return (
    <div className="mx-3 mb-2 mt-1 rounded-lg border border-purple-800/40 bg-purple-950/20 p-2.5 space-y-2 text-xs">
      <div className="flex items-center gap-2 text-gray-400">
        <span className="capitalize">{node.type || 'place'}</span>
        <span>· importance {node.importance ?? '?'}/10</span>
        {node.region && <span className="truncate">· {node.region}</span>}
      </div>
      {node.description ? (
        <p className="text-gray-300 leading-relaxed">{renderDescriptionWithLinks(node.description)}</p>
      ) : (
        <p className="text-gray-600 italic">No description yet — enrichment fills this in.</p>
      )}
      {node.additional_details && (
        <div className="border-t border-purple-900/40 pt-1.5">
          <div className="text-[10px] uppercase tracking-wide text-gray-500 mb-0.5">Details</div>
          <p className="text-gray-400 whitespace-pre-wrap leading-relaxed">{node.additional_details}</p>
        </div>
      )}
      {views.length > 0 && (
        <div className="border-t border-purple-900/40 pt-1.5 space-y-1">
          <div className="text-[10px] uppercase tracking-wide text-gray-500">Ways from here</div>
          {views.map(({ connection, far }, i) => (
            <button
              key={connection.id || i}
              onClick={() => far.map_id && onOpenMap(far.map_id, far.node_id)}
              className="w-full text-left text-gray-300 hover:text-purple-300 transition-colors"
            >
              <span
                className="inline-block w-2 h-2 rotate-45 mr-1.5"
                style={{ backgroundColor: CONNECTION_COLORS[connection.kind] || '#8b5cf6' }}
              />
              {connection.name || connection.kind || 'connection'}
              {connection.hidden && (
                <span className="ml-1 text-[10px] text-gray-500 border border-gray-700 rounded px-1">hidden</span>
              )}
              <span className="text-gray-500"> → {mapsById[far.map_id]?.label || far.map_id}</span>
            </button>
          ))}
        </div>
      )}
      {childMaps.length > 0 && (
        <div className="border-t border-purple-900/40 pt-1.5 space-y-1">
          {childMaps.map((m) => (
            <button
              key={m.map_id}
              onClick={() => onOpenMap(m.map_id)}
              className="w-full text-left text-purple-300 hover:text-purple-200 transition-colors"
            >
              ⤵ Enter {m.label || m.map_id}
              {m.level_type && <span className="text-gray-500"> ({m.level_type})</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * ElementsPanel — everything the current map contains, plus the design
 * notes bound to it. Selection syncs with the map (click a row → the map
 * zooms to it; tap a node on the map → its row highlights and expands).
 * Double-clicking a row that anchors a child hierarchy descends into it,
 * mirroring the map's own double-click.
 */
export default function ElementsPanel({ map, mapsById, connections, childAnchors, notes, selectedNodeId, onSelectNode, onOpenMap }) {
  const nodes = useMemo(() => {
    const list = [...(map?.nodes || [])];
    list.sort((a, b) => (b.importance ?? 0) - (a.importance ?? 0)
      || String(a.name || '').localeCompare(String(b.name || '')));
    return list;
  }, [map]);

  const byNode = useMemo(() => connectionsByNode(connections), [connections]);
  const regions = map?.regions || [];

  const childMapsFor = (nodeId) => {
    const ids = childAnchors[`${map?.map_id}:${nodeId}`] || [];
    return ids.map((id) => mapsById[id]).filter(Boolean);
  };

  // Connections with an endpoint on this map (ways in and out of it).
  const mapConnections = useMemo(() => {
    const seen = new Set();
    return (connections || []).filter((c) => {
      const touches = c.from?.map_id === map?.map_id || c.to?.map_id === map?.map_id;
      if (!touches || seen.has(c.id)) return false;
      seen.add(c.id);
      return true;
    });
  }, [connections, map]);

  // Keep the selected node's row in view when selection comes from the map.
  useEffect(() => {
    if (!selectedNodeId) return;
    const el = document.getElementById(`wb-el-node-${selectedNodeId}`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [selectedNodeId]);

  if (!map) {
    return <div className="p-4 text-sm text-gray-500">No map selected.</div>;
  }

  const named = nodes.filter((n) => n.name).length;

  return (
    <div className="text-sm">
      <div className="px-3 py-3 border-b border-gray-800">
        <div className="flex items-baseline gap-2">
          <h2 className="text-base font-bold text-gray-100 truncate">{map.label || map.map_id}</h2>
          {map.level_type && (
            <span className="text-[10px] uppercase tracking-wide text-purple-300/80 border border-purple-800/60 rounded px-1.5 py-0.5">
              {map.level_type}
            </span>
          )}
        </div>
        {map.description && (
          <p className="text-xs text-gray-400 mt-1.5 leading-relaxed">{map.description}</p>
        )}
        {map.parent_map_id && mapsById[map.parent_map_id] && (
          <button
            onClick={() => onOpenMap(map.parent_map_id, map.anchor_node_id)}
            className="mt-2 text-xs text-purple-400 hover:text-purple-300 transition-colors"
          >
            ↑ Up to {mapsById[map.parent_map_id].label || map.parent_map_id}
          </button>
        )}
      </div>

      {notes.length > 0 && (
        <Section title="Design notes here" count={notes.length}>
          <ul className="px-3 space-y-1.5">
            {notes.map((n) => (
              <li key={n.id || n.text} className="text-xs text-gray-300 leading-relaxed">
                <span className="text-amber-400/90">✎</span> {n.text}
                {noteStatusBadge(n)}
                {n.subject && <span className="text-gray-600"> — {n.subject}</span>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {regions.length > 0 && (
        <Section title="Regions" count={regions.length} defaultOpen={false}>
          <ul className="px-3 space-y-1">
            {regions.map((r, i) => (
              <li key={r.id || r.name || i} className="text-xs text-gray-300">
                {r.name || `Region ${i + 1}`}
                <span className="text-gray-600"> · {(r.node_ids || []).length} places</span>
                {r.description && (
                  <div className="text-gray-500 leading-relaxed">{r.description}</div>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Places" count={`${named} named / ${nodes.length}`}>
        <ul>
          {nodes.map((node) => {
            const isSelected = node.id === selectedNodeId;
            const children = childMapsFor(node.id);
            const views = byNode[node.id] || [];
            return (
              <li key={node.id} id={`wb-el-node-${node.id}`}>
                <button
                  onClick={() => onSelectNode(node)}
                  onDoubleClick={() => { if (children.length) onOpenMap(children[0].map_id); }}
                  title={children.length ? 'Double-click to enter' : undefined}
                  className={`w-full flex items-center gap-2 px-3 py-1.5 text-left transition-colors ${
                    isSelected ? 'bg-purple-900/40' : 'hover:bg-gray-800/50'
                  }`}
                >
                  <span
                    className="inline-block w-2.5 h-2.5 rounded-full shrink-0"
                    style={{ backgroundColor: TYPE_COLORS[node.type] || '#6b7280' }}
                  />
                  <span className={`flex-1 truncate text-xs ${node.name ? 'text-gray-200' : 'text-gray-500 italic'}`}>
                    {node.name || `Unnamed ${node.type || 'place'}`}
                  </span>
                  {views.some((v) => v.far.map_id !== map.map_id) && (
                    <span className="text-purple-400/80 text-[10px]" title="Has a way to another map">⇄</span>
                  )}
                  {children.length > 0 && (
                    <span className="text-purple-300 text-[10px]" title="Contains a deeper map — double-click to enter">▸</span>
                  )}
                  <span className="text-[10px] text-gray-600 w-4 text-right">{node.importance ?? ''}</span>
                </button>
                {isSelected && (
                  <NodeDetail
                    node={node}
                    views={views}
                    childMaps={children}
                    mapsById={mapsById}
                    onOpenMap={onOpenMap}
                  />
                )}
              </li>
            );
          })}
        </ul>
      </Section>

      {mapConnections.length > 0 && (
        <Section title="Ways in & out" count={mapConnections.length} defaultOpen={false}>
          <ul className="px-3 space-y-1">
            {mapConnections.map((c, i) => {
              const outbound = c.from?.map_id === map.map_id;
              const far = outbound ? c.to : c.from;
              return (
                <li key={c.id || i}>
                  <button
                    onClick={() => far?.map_id && onOpenMap(far.map_id, far.node_id)}
                    className="w-full text-left text-xs text-gray-300 hover:text-purple-300 transition-colors"
                  >
                    <span
                      className="inline-block w-2 h-2 rotate-45 mr-1.5"
                      style={{ backgroundColor: CONNECTION_COLORS[c.kind] || '#8b5cf6' }}
                    />
                    {c.name || c.kind || 'connection'}
                    {c.hidden && (
                      <span className="ml-1 text-[10px] text-gray-500 border border-gray-700 rounded px-1">hidden</span>
                    )}
                    <span className="text-gray-500">
                      {' '}{outbound ? '→' : '←'} {mapsById[far?.map_id]?.label || far?.map_id}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </Section>
      )}

      {/* Deeper maps anchored anywhere on this map, for quick descent. */}
      {(() => {
        const children = Object.values(mapsById).filter((m) => m.parent_map_id === map.map_id);
        if (!children.length) return null;
        return (
          <Section title="Deeper maps" count={children.length} defaultOpen={false}>
            <ul className="px-3 space-y-1">
              {children.map((m) => (
                <li key={m.map_id}>
                  <button
                    onClick={() => onOpenMap(m.map_id)}
                    className="w-full text-left text-xs text-purple-300 hover:text-purple-200 transition-colors"
                  >
                    ⤵ {m.label || m.map_id}
                    {m.level_type && <span className="text-gray-500"> ({m.level_type})</span>}
                    {m.anchor_node_id && (
                      <span className="text-gray-600">
                        {' '}at {(map.nodes || []).find((n) => n.id === m.anchor_node_id)?.name || m.anchor_node_id}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          </Section>
        );
      })()}
    </div>
  );
}
