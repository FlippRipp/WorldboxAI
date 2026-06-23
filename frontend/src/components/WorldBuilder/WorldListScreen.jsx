import { useState, useEffect, useCallback } from 'react';
import { api } from '../../lib/api';

export default function WorldListScreen({ onOpenWorld, onBack }) {
  const [worlds, setWorlds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [confirmDelete, setConfirmDelete] = useState(null);

  const fetchWorlds = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.listWorlds();
      setWorlds(data.worlds || []);
    } catch (e) {
      console.error('Failed to list worlds:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchWorlds(); }, [fetchWorlds]);

  const handleDelete = async (worldId) => {
    try {
      await api.deleteWorld(worldId);
      setConfirmDelete(null);
      fetchWorlds();
    } catch (e) {
      alert('Failed to delete world: ' + e.message);
    }
  };

  const handleCreate = () => {
    onOpenWorld(null);
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col p-6">
      <div className="w-full max-w-4xl mx-auto">
        <button
          onClick={onBack}
          className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-8"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Menu
        </button>

        <div className="flex items-center justify-between mb-6">
          <h2 className="text-3xl font-bold text-gray-100">Your Worlds</h2>
          <button
            onClick={handleCreate}
            className="px-4 py-2 bg-purple-600 hover:bg-purple-500 rounded-lg font-medium transition-colors flex items-center gap-2"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            Create New World
          </button>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-16">
            <span className="inline-block w-8 h-8 border-2 border-purple-400/30 border-t-purple-400 rounded-full animate-spin" />
          </div>
        ) : worlds.length === 0 ? (
          <div className="text-center py-16 bg-gray-800/40 border border-gray-700 rounded-xl">
            <div className="text-5xl mb-4">🌍</div>
            <h3 className="text-xl font-semibold text-gray-300 mb-2">No Worlds Yet</h3>
            <p className="text-gray-500 mb-6">Create your first world to get started.</p>
            <button
              onClick={handleCreate}
              className="px-6 py-2 bg-purple-600 hover:bg-purple-500 rounded-lg font-medium transition-colors"
            >
              Create New World
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {worlds.map((world) => (
              <div
                key={world.id}
                className="bg-gray-800/60 border border-gray-700 rounded-xl p-5 hover:border-purple-500/50 transition-colors"
              >
                <h3 className="text-lg font-semibold text-gray-100 mb-1 truncate">{world.name}</h3>
                <p className="text-xs text-gray-500 mb-3 line-clamp-2">{world.seed_prompt || 'No prompt'}</p>
                <div className="flex items-center gap-3 text-xs text-gray-500 mb-4">
                  <span>{world.step_count} steps</span>
                  {world.created_at && (
                    <>
                      <span>·</span>
                      <span>{new Date(world.created_at).toLocaleDateString()}</span>
                    </>
                  )}
                  {world.in_progress && (
                    <>
                      <span>·</span>
                      <span className="text-amber-400 font-medium">In Progress</span>
                    </>
                  )}
                </div>
                <div className="flex gap-2">
                  {world.in_progress ? (
                    <button
                      onClick={() => onOpenWorld(world.id, true)}
                      className="flex-1 px-3 py-1.5 bg-amber-700 hover:bg-amber-600 rounded text-sm font-medium transition-colors"
                    >
                      Resume
                    </button>
                  ) : (
                    <button
                      onClick={() => onOpenWorld(world.id)}
                      className="flex-1 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded text-sm font-medium transition-colors"
                    >
                      Open
                    </button>
                  )}
                  <button
                    onClick={() => setConfirmDelete(world.id)}
                    className="px-3 py-1.5 bg-gray-700 hover:bg-red-900/50 hover:text-red-300 rounded text-sm transition-colors"
                  >
                    Del
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {confirmDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" role="dialog" aria-modal="true">
          <div className="bg-gray-800 rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl">
            <h3 className="text-lg font-semibold mb-2">Delete World</h3>
            <p className="text-gray-300 text-sm mb-6">
              This permanently deletes the world and all its data. This action cannot be undone.
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setConfirmDelete(null)}
                className="px-4 py-2 text-sm rounded-lg bg-gray-700 hover:bg-gray-600 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => handleDelete(confirmDelete)}
                className="px-4 py-2 text-sm rounded-lg bg-red-600 hover:bg-red-500 text-white transition-colors"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
