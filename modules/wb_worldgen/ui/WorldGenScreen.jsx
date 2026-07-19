import React, { useState } from 'react';
import { api } from 'api';
import WorldListScreen from './WorldBuilder/WorldListScreen';
import WorldCreateScreen, { readSavedForm, pinAgentBuild } from './WorldBuilder/WorldCreateScreen';
import WorldExplorerScreen from './WorldExplorer/WorldExplorerScreen';

// Entry screen for the "World Generation" module mode: the world list, the
// create flow (ideation → agent build observer), and the world explorer.
export default function WorldGenScreen({ onBack }) {
  // A pinned agent build (running, or terminal and not yet dismissed) takes
  // the user straight back to its observer inside the create screen — the
  // loop runs server-side and must be findable after a relaunch. Synchronous
  // (localStorage) so the list never flashes first.
  const [view, setView] = useState(() => (readSavedForm().agentWorldId ? 'create' : 'list')); // 'list' | 'create' | 'explore'
  const [exploreWorldId, setExploreWorldId] = useState(null);
  const [createKey, setCreateKey] = useState(0);

  const openObserver = (worldId) => {
    pinAgentBuild(worldId);
    setCreateKey((k) => k + 1);
    setView('create');
  };

  // Recovery for an in-progress world: reattach to its recorded build's
  // observer, or — for worlds no agent build ever touched (interrupted or
  // pre-agent-era drafts) — adopt it into a fresh build that finishes it.
  // The adopted world's own seed prompt and brief win server-side; the
  // prompt passed here only seeds worlds that never recorded one.
  const handleRecover = async (world) => {
    if (world.has_agent_build) {
      openObserver(world.id);
      return;
    }
    try {
      const res = await api.agentBuild(
        world.seed_prompt || world.name || 'Finish building this world.',
        world.scenario_id || null, [], [], world.id);
      openObserver(res.world_id);
    } catch (e) {
      alert('Failed to start the finishing build: ' + e.message);
    }
  };

  if (view === 'create') {
    return (
      <WorldCreateScreen
        key={createKey}
        onBack={() => setView('list')}
        onOpenWorlds={() => setView('list')}
        onExploreWorld={(id) => { setExploreWorldId(id); setView('explore'); }}
      />
    );
  }

  if (view === 'explore') {
    return (
      <WorldExplorerScreen
        worldId={exploreWorldId}
        onBack={() => setView('list')}
      />
    );
  }

  return (
    <WorldListScreen
      onBack={onBack}
      onOpenWorld={(id) => {
        if (id) {
          setExploreWorldId(id);
          setView('explore');
        } else {
          setCreateKey((k) => k + 1);
          setView('create');
        }
      }}
      onRecoverWorld={handleRecover}
    />
  );
}
