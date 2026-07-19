import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { api } from 'api';
import MapRenderer from '../WorldBuilder/MapRenderer';
import { normalizeWorldData, breadcrumb, childrenByAnchor } from '../lib/mapspace';
import ElementsPanel from './ElementsPanel';
import GlobalPanel from './GlobalPanel';

// Loose client-side mirror of the server's join_key normalization (N2):
// good enough to *display* which map a subject note speaks about; the note
// lints and the verifier stay the authority on real binding.
export function joinKey(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function notesBoundToMap(notes, map) {
  if (!map) return [];
  const mapKeys = [joinKey(map.label), joinKey(map.map_id)].filter(Boolean);
  const nodeKeys = new Set((map.nodes || []).filter((n) => n.name).map((n) => joinKey(n.name)));
  return (notes || []).filter((n) => {
    const s = joinKey(n.subject);
    if (!s) return false;
    if (mapKeys.includes(s) || nodeKeys.has(s)) return true;
    // Containment tier: "the sand planet Kharos" ↔ map "Kharos".
    if (mapKeys.some((k) => k && (s.includes(k) || k.includes(s)))) return true;
    return [...nodeKeys].some((k) => k && (s.includes(k) || k.includes(s)));
  });
}

/**
 * WorldExplorerScreen — the post-creation home of a world.
 *
 * The map is the world: it fills the screen, explorable through the
 * renderer's pan/zoom and hierarchy navigation (double-click descends into
 * child maps). The elements panel on the left lists everything the current
 * map contains — nodes, regions, ways out, child maps, the design notes
 * bound to it — synced both ways with the map's selection. The panel on
 * the right holds the world's global entries (brief, lore, rules, factions,
 * terrain, enrichment, interiors), expandable in place, editable and
 * regenerable where that is safe on a saved world.
 *
 * Reads the COMPILED world — child-map bundles, surgery connections and
 * enrichment merged — so what it shows is the world as a session would
 * load it, not the raw generation-step snapshot.
 */
export default function WorldExplorerScreen({ worldId, onBack }) {
  const [compiled, setCompiled] = useState(null);
  const [worldState, setWorldState] = useState(null); // raw step state, for the editing forms
  const [pipeline, setPipeline] = useState([]);
  const [error, setError] = useState(null);
  const [activeMapId, setActiveMapId] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [focusNodeId, setFocusNodeId] = useState(null);
  const isDesktop = () => (typeof window !== 'undefined' ? window.innerWidth >= 1024 : true);
  const [leftOpen, setLeftOpen] = useState(isDesktop);
  const [rightOpen, setRightOpen] = useState(isDesktop);
  const focusTimer = useRef(null);

  const refresh = useCallback(async () => {
    const [compiledRes, stateRes] = await Promise.all([
      api.getCompiledWorld(worldId),
      api.loadWorld(worldId),
    ]);
    setCompiled(compiledRes.compiled);
    setWorldState(stateRes.state);
  }, [worldId]);

  useEffect(() => {
    if (!worldId) return;
    setError(null);
    setCompiled(null);
    refresh().catch((e) => setError(e.message));
  }, [worldId, refresh]);

  useEffect(() => {
    api.getWorldPipeline()
      .then((d) => setPipeline(d.pipeline || []))
      .catch(() => {});
    return () => { if (focusTimer.current) clearTimeout(focusTimer.current); };
  }, []);

  const { mapsById, rootMapId, connections } = useMemo(
    // The explorer is the AUTHOR's view: hidden (undiscovered) connections
    // stay visible here, badged — only the player's overlay filters them.
    () => normalizeWorldData(compiled),
    [compiled],
  );
  const hasMaps = Object.keys(mapsById).length > 0;

  useEffect(() => {
    // Seed the active map, and recover when it vanishes (a regeneration or
    // an expansion can reshape the map space under the view).
    if (hasMaps && (!activeMapId || !mapsById[activeMapId])) {
      setActiveMapId(rootMapId);
    }
  }, [hasMaps, mapsById, rootMapId, activeMapId]);

  const activeMap = (activeMapId && mapsById[activeMapId]) || null;
  const crumbs = useMemo(
    () => (activeMapId ? breadcrumb(mapsById, activeMapId) : []),
    [mapsById, activeMapId],
  );
  const childAnchors = useMemo(() => childrenByAnchor(mapsById), [mapsById]);
  const briefNotes = compiled?.brief?.notes || [];
  const mapNotes = useMemo(
    () => notesBoundToMap(briefNotes, activeMap),
    [briefNotes, activeMap],
  );

  // Focus is transient: zoom the renderer in on the node, then clear so
  // later map switches reset to their default view (GameMapOverlay pattern).
  const focusOn = (nodeId) => {
    setFocusNodeId(nodeId);
    if (focusTimer.current) clearTimeout(focusTimer.current);
    focusTimer.current = setTimeout(() => setFocusNodeId(null), 4000);
  };

  const handleMapChange = (targetMapId, nodeId) => {
    setActiveMapId(targetMapId);
    setSelectedNodeId(nodeId || null);
    if (nodeId) focusOn(nodeId);
  };

  const handleSelectFromList = (node) => {
    setSelectedNodeId(node.id);
    focusOn(node.id);
  };

  const worldName = compiled?.lore?.world_name
    || worldState?.steps?.lore?.data?.world_name || worldId;
  const inProgress = worldState && worldState.complete === false;

  if (error) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center justify-center p-6">
        <p className="text-red-400 mb-4">Failed to load world: {error}</p>
        <button onClick={onBack} className="text-purple-400 hover:text-purple-300">Go back</button>
      </div>
    );
  }

  if (!compiled) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex items-center justify-center">
        <span className="inline-block w-8 h-8 border-2 border-purple-400/30 border-t-purple-400 rounded-full animate-spin" />
      </div>
    );
  }

  const panelToggle = (open, setOpen, label, side) => (
    <button
      onClick={() => setOpen((v) => !v)}
      title={`${open ? 'Hide' : 'Show'} ${label}`}
      className={`px-2 py-1 rounded text-xs border transition-colors ${
        open
          ? 'border-purple-500/60 text-purple-300 bg-purple-900/30'
          : 'border-gray-700 text-gray-400 hover:bg-gray-700'
      }`}
    >
      {side === 'left' ? (open ? '◀' : '▶') : (open ? '▶' : '◀')} {label}
    </button>
  );

  return (
    <div className="h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col overflow-hidden">
      <header className="shrink-0 flex items-center gap-3 px-4 py-2.5 border-b border-gray-800 bg-gray-900/70">
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 text-gray-400 hover:text-gray-200 transition-colors text-sm"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Worlds
        </button>
        <div className="flex-1 min-w-0 flex items-baseline gap-2">
          <h1 className="text-base font-bold text-gray-100 truncate">{worldName}</h1>
          {crumbs.length > 0 && (
            <span className="text-xs text-gray-500 truncate hidden sm:inline">
              {crumbs.map((m, i) => (
                <span key={m.map_id}>
                  {i > 0 && ' › '}
                  <button
                    onClick={() => handleMapChange(m.map_id)}
                    className={`hover:text-purple-300 ${m.map_id === activeMapId ? 'text-gray-300' : ''}`}
                  >
                    {m.label || m.map_id}
                  </button>
                </span>
              ))}
            </span>
          )}
          {inProgress && (
            <span className="shrink-0 text-[10px] uppercase tracking-wide text-amber-400 border border-amber-700/60 rounded px-1.5 py-0.5">
              In progress
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {panelToggle(leftOpen, setLeftOpen, 'Elements', 'left')}
          {panelToggle(rightOpen, setRightOpen, 'World Info', 'right')}
        </div>
      </header>

      <div className="flex-1 flex overflow-hidden relative">
        {/* Left: everything the current map contains. Static column on
            desktop, slide-over drawer on small screens. */}
        {leftOpen && (
          <>
            <div
              className="lg:hidden absolute inset-0 bg-black/50 z-20"
              onClick={() => setLeftOpen(false)}
            />
            <aside className="absolute lg:static inset-y-0 left-0 z-30 w-[85vw] max-w-sm lg:w-80 shrink-0 bg-gray-900/95 lg:bg-gray-900/50 border-r border-gray-800 overflow-y-auto">
              <ElementsPanel
                map={activeMap}
                mapsById={mapsById}
                connections={connections}
                childAnchors={childAnchors}
                notes={mapNotes}
                selectedNodeId={selectedNodeId}
                onSelectNode={handleSelectFromList}
                onOpenMap={handleMapChange}
              />
            </aside>
          </>
        )}

        <main className="flex-1 min-w-0">
          {hasMaps ? (
            <MapRenderer
              mapsById={mapsById}
              connections={connections}
              rootMapId={rootMapId}
              worldId={worldId}
              activeMapId={activeMapId}
              onMapChange={handleMapChange}
              focusNodeId={focusNodeId}
              onNodeSelect={(node) => setSelectedNodeId(node ? node.id : null)}
            />
          ) : (
            <div className="h-full flex items-center justify-center text-gray-500 text-sm p-8 text-center">
              This world has no maps yet.
              {inProgress ? ' Finish the build from the Worlds list to generate them.' : ''}
            </div>
          )}
        </main>

        {/* Right: the world's global information entries. */}
        {rightOpen && (
          <>
            <div
              className="lg:hidden absolute inset-0 bg-black/50 z-20"
              onClick={() => setRightOpen(false)}
            />
            <aside className="absolute lg:static inset-y-0 right-0 z-30 w-[85vw] max-w-sm lg:w-96 shrink-0 bg-gray-900/95 lg:bg-gray-900/50 border-l border-gray-800 overflow-y-auto">
              <GlobalPanel
                worldId={worldId}
                compiled={compiled}
                worldState={worldState}
                pipeline={pipeline}
                mapsById={mapsById}
                onChanged={() => refresh().catch((e) => setError(e.message))}
              />
            </aside>
          </>
        )}
      </div>
    </div>
  );
}
