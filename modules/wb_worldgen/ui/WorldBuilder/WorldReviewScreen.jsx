import { useState, useEffect } from 'react';
import { api } from 'api';
import StepCard from './StepCard';

const EXPANDABLE_TYPES = new Set(['city', 'settlement', 'port', 'stronghold']);

function _mapNodes(mapData) {
  if (!mapData) return [];
  if (mapData.layers) return mapData.layers.flatMap((l) => l.map?.nodes || []);
  return mapData.nodes || [];
}

// Pre-bake interior detail (districts/venues) for key locations so it's
// already cached when the story reaches them. Entirely optional — anything
// left unexpanded generates on demand during play.
function SiteExpansionCard({ worldId, worldState }) {
  const [sites, setSites] = useState({});
  const [busyId, setBusyId] = useState(null);

  useEffect(() => {
    if (!worldId) return;
    api.getWorldSites(worldId)
      .then((data) => setSites(data.sites || {}))
      .catch(() => {});
  }, [worldId]);

  const mapData = worldState?.steps?.map_generation?.data;
  const majors = _mapNodes(mapData).filter(
    (n) => n.name && EXPANDABLE_TYPES.has(n.type) && (n.importance ?? 0) >= 6,
  );
  if (!majors.length) return null;

  const expand = async (nodeId) => {
    setBusyId(nodeId);
    try {
      const res = await api.expandWorldSite(worldId, nodeId);
      setSites((prev) => ({ ...prev, [nodeId]: res.site }));
    } catch (e) {
      alert('Expansion failed: ' + e.message);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="bg-gray-800/60 border border-gray-700 rounded-lg p-4 space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-gray-200">Location Interiors</h3>
        <p className="text-xs text-gray-500 mt-1">
          Optionally pre-generate the interior (layout, districts, venues) of key
          locations. Anything left alone is generated automatically the first time
          the story goes there.
        </p>
      </div>
      <ul className="space-y-1.5">
        {majors.map((n) => {
          const site = sites[n.id];
          return (
            <li key={n.id} className="flex items-center justify-between gap-2 text-xs">
              <span className="text-gray-300">
                <span className="text-amber-400">{n.name}</span>
                <span className="text-gray-500"> ({n.type})</span>
                {site && (
                  <span className="text-gray-500"> — {site.sub_locations?.length || 0} places inside</span>
                )}
              </span>
              {site ? (
                <span className="text-emerald-400">Expanded</span>
              ) : (
                <button
                  onClick={() => expand(n.id)}
                  disabled={busyId !== null}
                  className="px-2 py-1 bg-purple-700 hover:bg-purple-600 disabled:opacity-50 rounded text-[11px] text-gray-100 transition-colors"
                >
                  {busyId === n.id ? 'Expanding...' : 'Expand'}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export default function WorldReviewScreen({ worldId, onBack, onEnterGame }) {
  const [pipeline, setPipeline] = useState([]);
  const [worldState, setWorldState] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.getWorldPipeline()
      .then((data) => setPipeline(data.pipeline || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!worldId) return;
    setLoading(true);
    api.loadWorld(worldId)
      .then((data) => setWorldState(data.state))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [worldId]);

  const handleReroll = async (stepId) => {
    const stepState = worldState?.steps?.[stepId];
    const note = stepState?.note || '';
    setLoading(true);
    try {
      const result = await api.generateWorldStep(stepId, note);
      setWorldState(result.state);
    } catch (e) {
      alert('Re-roll failed: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async (stepId, editedData) => {
    if (!worldId) return;
    const stepEntry = worldState?.steps?.[stepId];
    const dataToSave = { ...stepEntry, data: editedData };
    setLoading(true);
    try {
      await api.saveWorldStep(worldId, stepId, dataToSave);
      setWorldState(prev => ({
        ...prev,
        steps: {
          ...prev.steps,
          [stepId]: dataToSave,
        },
      }));
    } catch (e) {
      alert('Failed to save step: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  if (!worldId) return null;

  if (loading && !worldState) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex items-center justify-center">
        <span className="inline-block w-8 h-8 border-2 border-purple-400/30 border-t-purple-400 rounded-full animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center justify-center p-6">
        <p className="text-red-400 mb-4">Failed to load world: {error}</p>
        <button onClick={onBack} className="text-purple-400 hover:text-purple-300">Go back</button>
      </div>
    );
  }

  const worldName = worldState?.steps?.lore?.data?.world_name || worldId;

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col p-6">
      <div className="w-full max-w-3xl mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <button
            onClick={onBack}
            className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
            Back to Worlds
          </button>
          <h2 className="text-xl font-bold text-gray-100">{worldName}</h2>
          <div className="w-24" />
        </div>

        <SiteExpansionCard worldId={worldId} worldState={worldState} />

        {pipeline.map((step) => {
          const stepState = worldState?.steps?.[step.id];
          if (!stepState?.data) return null;

          return (
            <StepCard
              key={step.id}
              step={step}
              state={stepState}
              onApprove={() => {}}
              onReroll={() => handleReroll(step.id)}
              onAddNote={() => {}}
              loading={loading}
              worldId={worldId}
              worldState={worldState}
            />
          );
        })}

        {pipeline.length === 0 && (
          <p className="text-center text-gray-500 py-12">No pipeline steps registered.</p>
        )}
      </div>
    </div>
  );
}
