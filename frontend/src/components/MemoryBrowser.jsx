import { useState, useEffect, useMemo } from 'react';
import { api } from '../lib/api';

export default function MemoryBrowser({ isOpen, onClose }) {
  const [memories, setMemories] = useState([]);
  const [activeIds, setActiveIds] = useState([]);
  const [contextQuery, setContextQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [deleteId, setDeleteId] = useState(null);

  const [filterEntity, setFilterEntity] = useState('');
  const [filterTopic, setFilterTopic] = useState('');
  const [sortBy, setSortBy] = useState('newest');

  const fetchMemories = () => {
    setLoading(true);
    setError('');
    api.getMemories()
      .then(data => {
        setMemories(data.memories || []);
        setActiveIds(data.active_ids || []);
        setContextQuery(data.context_query || '');
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (isOpen) fetchMemories();
  }, [isOpen]);

  const allEntities = useMemo(() => {
    const s = new Set();
    memories.forEach(m => (m.entities || []).forEach(e => s.add(e)));
    return [...s].sort();
  }, [memories]);

  const allTopics = useMemo(() => {
    const s = new Set();
    memories.forEach(m => (m.topics || []).forEach(t => s.add(t)));
    return [...s].sort();
  }, [memories]);

  const filtered = useMemo(() => {
    let list = [...memories];
    if (filterEntity) list = list.filter(m => (m.entities || []).includes(filterEntity));
    if (filterTopic) list = list.filter(m => (m.topics || []).includes(filterTopic));
    if (sortBy === 'newest') list.sort((a, b) => (b.turn_generated || 0) - (a.turn_generated || 0));
    else if (sortBy === 'importance') list.sort((a, b) => (b.importance || 0) - (a.importance || 0));
    else if (sortBy === 'oldest') list.sort((a, b) => (a.turn_generated || 0) - (b.turn_generated || 0));
    return list;
  }, [memories, filterEntity, filterTopic, sortBy]);

  const activeMemories = filtered.filter(m => activeIds.includes(m.id));
  const inactiveMemories = filtered.filter(m => !activeIds.includes(m.id));
  const permanentCount = memories.filter(m => m.permanent).length;
  const activeCount = activeIds.length;

  const handleDelete = async (id) => {
    try {
      await api.deleteMemory(id);
      setMemories(prev => prev.filter(m => m.id !== id));
      setActiveIds(prev => prev.filter(aid => aid !== id));
      setDeleteId(null);
    } catch (e) {
      setError(e.message);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" role="dialog" aria-modal="true" aria-label="Memory browser">
      <div className="bg-gray-800 w-full max-w-2xl rounded-lg shadow-2xl border border-gray-700 flex flex-col max-h-[90vh]">
        <div className="p-4 border-b border-gray-700 flex justify-between items-center bg-gray-900 rounded-t-lg">
          <div>
            <h2 className="text-xl font-bold text-gray-100">Memory Browser</h2>
            <p className="text-xs text-gray-500 mt-1">
              {memories.length} memories{activeCount > 0 && ` · ${activeCount} active this turn`}{permanentCount > 0 && ` · ${permanentCount} permanent`}
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-2xl leading-none" aria-label="Close">&times;</button>
        </div>

        <div className="p-4 border-b border-gray-700 bg-gray-850 flex flex-wrap gap-2 items-center">
          <select
            value={filterEntity}
            onChange={e => setFilterEntity(e.target.value)}
            className="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-sm text-gray-200"
            aria-label="Filter by entity"
          >
            <option value="">All entities</option>
            {allEntities.map(e => <option key={e} value={e}>{e}</option>)}
          </select>
          <select
            value={filterTopic}
            onChange={e => setFilterTopic(e.target.value)}
            className="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-sm text-gray-200"
            aria-label="Filter by topic"
          >
            <option value="">All topics</option>
            {allTopics.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          <select
            value={sortBy}
            onChange={e => setSortBy(e.target.value)}
            className="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-sm text-gray-200"
            aria-label="Sort memories"
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="importance">Highest importance</option>
          </select>
          <button
            onClick={fetchMemories}
            className="ml-auto text-sm text-purple-400 hover:text-purple-300 px-2 py-1"
            aria-label="Refresh memories"
          >
            Refresh
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {loading && <div className="text-center text-gray-400 py-8">Loading memories...</div>}
          {error && <div className="bg-red-900/40 border border-red-700/50 rounded p-3 text-red-200 text-sm">{error}</div>}

          {!loading && !error && filtered.length === 0 && (
            <div className="text-center text-gray-500 py-12">
              <div className="text-4xl mb-3">&#129504;</div>
              <p>No memories yet</p>
              <p className="text-sm mt-1">Play a few turns with LLM_MODE=live to generate memories, or use mock mode for test entries.</p>
            </div>
          )}

          {contextQuery && activeMemories.length > 0 && (
            <div className="mb-4">
              <div className="text-xs text-purple-400 font-semibold uppercase tracking-wide mb-2">
                Active This Turn · triggered by "{contextQuery.length > 60 ? contextQuery.slice(0, 60) + '...' : contextQuery}"
              </div>
              {activeMemories.map(m => (
                <MemoryCard key={m.id} memory={m} isActive onDelete={setDeleteId} />
              ))}
            </div>
          )}

          {inactiveMemories.length > 0 && (
            <div>
              {activeMemories.length > 0 && (
                <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">All Memories</div>
              )}
              {inactiveMemories.map(m => (
                <MemoryCard key={m.id} memory={m} onDelete={setDeleteId} />
              ))}
            </div>
          )}
        </div>

        <div className="p-3 border-t border-gray-700 bg-gray-900 rounded-b-lg flex justify-end">
          <button onClick={onClose} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded font-medium transition-colors text-sm">
            Close
          </button>
        </div>
      </div>

      {deleteId && (
        <DeleteConfirm
          onConfirm={() => handleDelete(deleteId)}
          onCancel={() => setDeleteId(null)}
        />
      )}
    </div>
  );
}

function MemoryCard({ memory, isActive, onDelete }) {
  const [expanded, setExpanded] = useState(false);
  const importancePct = ((memory.importance || 5) / 10) * 100;

  return (
    <div className={`bg-gray-900/60 rounded border p-3 mb-2 transition-colors ${isActive ? 'border-purple-500/50 ring-1 ring-purple-500/20' : 'border-gray-700 hover:border-gray-600'}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap mb-1">
            {memory.permanent && (
              <span className="text-amber-400 text-xs" title="Permanent - will never decay">&#128274;</span>
            )}
            {memory.turn_range && (
              <span className="text-xs text-gray-500 font-mono">{memory.turn_range}</span>
            )}
            <div className="flex items-center gap-0.5" title={`Importance: ${memory.importance}/10`}>
              {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(i => (
                <span key={i} className={`text-xs ${i <= (memory.importance || 5) ? 'text-yellow-400' : 'text-gray-600'}`}>
                  &#9733;
                </span>
              ))}
            </div>
          </div>

          <p
            className="text-sm text-gray-200 leading-relaxed cursor-pointer"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? memory.summary : (memory.summary || '').slice(0, 120) + ((memory.summary || '').length > 120 ? '...' : '')}
          </p>

          {expanded && memory.reason && (
            <p className="text-xs text-gray-500 mt-1 italic">Reason: {memory.reason}</p>
          )}
        </div>

        <button
          onClick={(e) => { e.stopPropagation(); onDelete(memory.id); }}
          className="text-gray-600 hover:text-red-400 text-sm shrink-0 px-1"
          title="Delete memory"
          aria-label={`Delete memory ${memory.id}`}
        >
          &#128465;
        </button>
      </div>

      {(memory.entities?.length > 0 || memory.topics?.length > 0) && (
        <div className="flex flex-wrap gap-1 mt-2">
          {(memory.entities || []).map(e => (
            <span key={e} className="text-[10px] px-1.5 py-0.5 rounded bg-blue-900/40 text-blue-300 border border-blue-800/50">
              {e}
            </span>
          ))}
          {(memory.topics || []).map(t => (
            <span key={t} className="text-[10px] px-1.5 py-0.5 rounded bg-green-900/40 text-green-300 border border-green-800/50">
              {t}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function DeleteConfirm({ onConfirm, onCancel }) {
  return (
    <div className="absolute inset-0 flex items-center justify-center bg-black/40" role="alertdialog" aria-modal="true">
      <div className="bg-gray-800 border border-red-700/50 rounded-lg p-5 shadow-xl max-w-sm">
        <p className="text-gray-200 text-sm mb-4">Delete this memory? This action cannot be undone.</p>
        <div className="flex justify-end gap-2">
          <button onClick={onCancel} className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors">
            Cancel
          </button>
          <button onClick={onConfirm} className="px-3 py-1.5 bg-red-700 hover:bg-red-600 text-white rounded text-sm transition-colors">
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}
