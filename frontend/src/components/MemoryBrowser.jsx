import { useState, useEffect, useMemo } from 'react';
import { api } from '../lib/api';

const TABS = [
  { id: 'active', label: 'Active' },
  { id: 'memories', label: 'Memories' },
  { id: 'world', label: 'World' },
  { id: 'lorebooks', label: 'Lorebooks' },
  { id: 'rag_debug', label: 'RAG Debug' },
];

// Badge colors per world-entry source type; unknown types fall back to gray.
const TYPE_COLORS = {
  lore: 'bg-purple-900/40 text-purple-300 border-purple-800/50',
  era: 'bg-indigo-900/40 text-indigo-300 border-indigo-800/50',
  region: 'bg-green-900/40 text-green-300 border-green-800/50',
  landmark: 'bg-teal-900/40 text-teal-300 border-teal-800/50',
  faction: 'bg-red-900/40 text-red-300 border-red-800/50',
  node: 'bg-sky-900/40 text-sky-300 border-sky-800/50',
  layer: 'bg-amber-900/40 text-amber-300 border-amber-800/50',
  connection: 'bg-gray-800 text-gray-300 border-gray-600',
  lorebook: 'bg-yellow-900/40 text-yellow-300 border-yellow-800/50',
};

const ACTIVE_CARD = 'border-purple-500/50 ring-1 ring-purple-500/20';

export default function MemoryBrowser({ isOpen, onClose, saveId }) {
  const [tab, setTab] = useState('active');
  const [search, setSearch] = useState('');

  const [memories, setMemories] = useState([]);
  const [activeIds, setActiveIds] = useState([]);
  const [contextQuery, setContextQuery] = useState('');
  const [worldEntries, setWorldEntries] = useState([]);
  const [worldActiveIds, setWorldActiveIds] = useState([]);
  const [stickyMap, setStickyMap] = useState({}); // source_id -> last active turn
  const [turnNo, setTurnNo] = useState(0);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [deleteId, setDeleteId] = useState(null);
  const [editing, setEditing] = useState(null); // {title, fields, initialValues, onSave}

  const fetchAll = () => {
    setLoading(true);
    setError('');
    Promise.all([api.getMemories(), api.getWorldEntries()])
      .then(([mem, world]) => {
        setMemories(mem.memories || []);
        setActiveIds(mem.active_ids || []);
        setContextQuery(mem.context_query || world.context_query || '');
        setWorldEntries(world.entries || []);
        setWorldActiveIds(world.active_ids || []);
        setStickyMap(world.sticky_source_ids || {});
        setTurnNo(world.turn || 0);
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    if (isOpen) fetchAll();
  }, [isOpen]);

  const refetchWorldEntries = () => {
    api.getWorldEntries()
      .then(world => {
        setWorldEntries(world.entries || []);
        setWorldActiveIds(world.active_ids || []);
        setStickyMap(world.sticky_source_ids || {});
        setTurnNo(world.turn || 0);
      })
      .catch(e => setError(e.message));
  };

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

  const editMemory = (memory) => setEditing({
    title: 'Edit Memory',
    fields: [
      { key: 'summary', label: 'Summary (what retrieval matches on — editing re-embeds)', type: 'textarea' },
      { key: 'text', label: 'Full text (what gets injected into the story context)', type: 'textarea' },
      { key: 'importance', label: 'Importance (1-10)', type: 'number', min: 1, max: 10 },
      { key: 'permanent', label: 'Permanent (never decays)', type: 'checkbox' },
    ],
    initialValues: {
      summary: memory.summary || '',
      text: memory.text || '',
      importance: memory.importance || 5,
      permanent: !!memory.permanent,
    },
    onSave: async (values, changed) => {
      if (Object.keys(changed).length === 0) return;
      const { memory: updated } = await api.updateMemory(memory.id, changed);
      setMemories(prev => prev.map(m => (m.id === memory.id ? updated : m)));
    },
  });

  const editWorldEntry = (entry) => setEditing({
    title: `Edit World Entry — ${entry.source_id || entry.source_type}`,
    fields: [
      { key: 'text', label: 'Entry text (saving re-embeds it)', type: 'textarea', rows: 6 },
    ],
    initialValues: { text: entry.text || '' },
    onSave: async (values, changed) => {
      if (!('text' in changed)) return;
      const { entry: updated } = await api.updateWorldEntry(entry.id, values.text);
      setWorldEntries(prev => prev.map(e => (e.id === entry.id ? updated : e)));
    },
  });

  if (!isOpen) return null;

  const counts = {
    memories: memories.length,
    world: worldEntries.filter(e => e.source_type !== 'lorebook').length,
    lorebooks: worldEntries.filter(e => e.source_type === 'lorebook').length,
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70" role="dialog" aria-modal="true" aria-label="Memory browser">
      <div className="bg-gray-800 w-full max-w-3xl rounded-lg shadow-2xl border border-gray-700 flex flex-col max-h-[90vh]">
        <div className="p-4 border-b border-gray-700 flex justify-between items-center bg-gray-900 rounded-t-lg">
          <div>
            <h2 className="text-xl font-bold text-gray-100">Memory Browser</h2>
            <p className="text-xs text-gray-500 mt-1">
              {counts.memories} memories · {counts.world} world entries · {counts.lorebooks} lorebook entries
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-white text-2xl leading-none" aria-label="Close">&times;</button>
        </div>

        <div className="px-4 pt-3 border-b border-gray-700 bg-gray-850 flex items-center gap-1">
          {TABS.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-4 py-2 text-sm font-medium rounded-t border-b-2 transition-colors ${
                tab === t.id
                  ? 'text-purple-300 border-purple-500 bg-gray-800'
                  : 'text-gray-400 border-transparent hover:text-gray-200'
              }`}
            >
              {t.label}
            </button>
          ))}
          <div className="ml-auto flex items-center gap-2 pb-2">
            {tab !== 'rag_debug' && (
              <input
                type="text"
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="Search..."
                className="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-sm text-gray-200 w-40"
                aria-label="Search entries"
              />
            )}
            <button
              onClick={fetchAll}
              className="text-sm text-purple-400 hover:text-purple-300 px-2 py-1"
              aria-label="Refresh entries"
            >
              Refresh
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {loading && <div className="text-center text-gray-400 py-8">Loading...</div>}
          {error && <div className="bg-red-900/40 border border-red-700/50 rounded p-3 text-red-200 text-sm">{error}</div>}

          {!loading && tab === 'active' && (
            <ActiveTab
              memories={memories}
              activeIds={activeIds}
              worldEntries={worldEntries}
              worldActiveIds={worldActiveIds}
              contextQuery={contextQuery}
              stickyMap={stickyMap}
              turn={turnNo}
              search={search}
              onDeleteMemory={setDeleteId}
              onEditMemory={editMemory}
              onEditWorldEntry={editWorldEntry}
            />
          )}
          {!loading && tab === 'memories' && (
            <MemoriesTab
              memories={memories}
              search={search}
              onDelete={setDeleteId}
              onEdit={editMemory}
            />
          )}
          {!loading && tab === 'world' && (
            <WorldTab
              entries={worldEntries.filter(e => e.source_type !== 'lorebook')}
              search={search}
              onEdit={editWorldEntry}
            />
          )}
          {!loading && tab === 'lorebooks' && (
            <LorebooksTab
              saveId={saveId}
              search={search}
              setEditing={setEditing}
              onEntriesChanged={refetchWorldEntries}
            />
          )}
          {!loading && tab === 'rag_debug' && (
            <RagDebugTab contextQuery={contextQuery} />
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
      {editing && (
        <EditEntryModal
          {...editing}
          onCancel={() => setEditing(null)}
          onDone={() => setEditing(null)}
        />
      )}
    </div>
  );
}

// ── Active tab ───────────────────────────────────────────────────────────────
// Everything currently injected into the storyteller's context, combined in
// one place: entries retrieved last turn, sticky lorebook entries still in
// their active window, and constant (always-on) lore.

function ActiveTab({ memories, activeIds, worldEntries, worldActiveIds, contextQuery,
                     stickyMap, turn, search, onDeleteMemory, onEditMemory, onEditWorldEntry }) {
  const q = search.trim().toLowerCase();
  const matches = (...texts) => !q || texts.some(t => (t || '').toLowerCase().includes(q));

  // Turns an entry has left in context: its stored expiry is the last turn
  // number it will still be injected on.
  const stickyLeft = (sourceId) => {
    const expires = stickyMap[sourceId];
    return typeof expires === 'number' ? Math.max(0, expires - turn + 1) : 0;
  };

  const activeWorld = worldEntries.filter(e =>
    worldActiveIds.includes(e.id) && matches(e.text, e.source_id));
  const activeMemories = memories.filter(m =>
    activeIds.includes(m.id) && matches(m.summary, m.text));
  const constants = worldEntries.filter(e =>
    e.source_type === 'lorebook' && e.constant && matches(e.text, e.source_id));

  const total = activeWorld.length + activeMemories.length + constants.length;

  return (
    <div>
      <p className="text-xs text-gray-500 mb-3">
        Everything currently injected into the storyteller's context: entries retrieved last turn,
        sticky lorebook entries still in their active window, and constant lore that is always included.
      </p>

      {total === 0 && (
        <div className="text-center text-gray-500 py-12">
          <div className="text-4xl mb-3">&#10024;</div>
          <p>Nothing active{q ? ' matches the search' : ' yet'}.</p>
          {!q && <p className="text-sm mt-1">Play a turn and the retrieved context will show up here.</p>}
        </div>
      )}

      {(activeWorld.length > 0 || activeMemories.length > 0) && (
        <TriggeredHeading contextQuery={contextQuery} />
      )}

      {activeWorld.length > 0 && (
        <div className="mb-4">
          <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">World Knowledge &amp; Lore</div>
          {activeWorld.map(e => (
            <WorldEntryCard
              key={e.id}
              entry={e}
              isActive
              stickyLeft={stickyLeft(e.source_id)}
              onEdit={onEditWorldEntry}
            />
          ))}
        </div>
      )}

      {activeMemories.length > 0 && (
        <div className="mb-4">
          <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">Memories</div>
          {activeMemories.map(m => (
            <MemoryCard key={m.id} memory={m} isActive onDelete={onDeleteMemory} onEdit={onEditMemory} />
          ))}
        </div>
      )}

      {constants.length > 0 && (
        <div>
          <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">Always in Context</div>
          {constants.map(e => (
            <WorldEntryCard key={e.id} entry={e} onEdit={onEditWorldEntry} />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Memories tab ─────────────────────────────────────────────────────────────

function MemoriesTab({ memories, search, onDelete, onEdit }) {
  const [filterEntity, setFilterEntity] = useState('');
  const [filterTopic, setFilterTopic] = useState('');
  const [sortBy, setSortBy] = useState('newest');

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
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      list = list.filter(m =>
        (m.summary || '').toLowerCase().includes(q) ||
        (m.text || '').toLowerCase().includes(q) ||
        (m.entities || []).some(e => e.toLowerCase().includes(q)) ||
        (m.topics || []).some(t => t.toLowerCase().includes(q))
      );
    }
    if (sortBy === 'newest') list.sort((a, b) => (b.turn_generated || 0) - (a.turn_generated || 0));
    else if (sortBy === 'importance') list.sort((a, b) => (b.importance || 0) - (a.importance || 0));
    else if (sortBy === 'oldest') list.sort((a, b) => (a.turn_generated || 0) - (b.turn_generated || 0));
    return list;
  }, [memories, filterEntity, filterTopic, sortBy, search]);

  return (
    <div>
      <div className="flex flex-wrap gap-2 items-center mb-3">
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
      </div>

      {filtered.length === 0 && (
        <div className="text-center text-gray-500 py-12">
          <div className="text-4xl mb-3">&#129504;</div>
          <p>No memories{memories.length > 0 ? ' match the filters' : ' yet'}</p>
          {memories.length === 0 && (
            <p className="text-sm mt-1">Play a few turns with LLM_MODE=live to generate memories, or use mock mode for test entries.</p>
          )}
        </div>
      )}

      {filtered.map(m => (
        <MemoryCard key={m.id} memory={m} onDelete={onDelete} onEdit={onEdit} />
      ))}
    </div>
  );
}

function MemoryCard({ memory, isActive, onDelete, onEdit }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className={`bg-gray-900/60 rounded border p-3 mb-2 transition-colors ${isActive ? ACTIVE_CARD : 'border-gray-700 hover:border-gray-600'}`}>
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

        <EditButton onClick={() => onEdit(memory)} label={`Edit memory ${memory.id}`} />
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

// ── World tab ────────────────────────────────────────────────────────────────

function WorldTab({ entries, search, onEdit }) {
  const [filterType, setFilterType] = useState('');
  const [filterRegion, setFilterRegion] = useState('');

  const allTypes = useMemo(() => [...new Set(entries.map(e => e.source_type))].sort(), [entries]);
  const allRegions = useMemo(
    () => [...new Set(entries.map(e => e.region).filter(Boolean))].sort(),
    [entries]
  );

  const filtered = useMemo(() => {
    let list = entries;
    if (filterType) list = list.filter(e => e.source_type === filterType);
    if (filterRegion) list = list.filter(e => e.region === filterRegion);
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      list = list.filter(e =>
        (e.text || '').toLowerCase().includes(q) ||
        (e.source_id || '').toLowerCase().includes(q) ||
        (e.region || '').toLowerCase().includes(q)
      );
    }
    return list;
  }, [entries, filterType, filterRegion, search]);

  return (
    <div>
      <div className="flex flex-wrap gap-2 items-center mb-3">
        <select
          value={filterType}
          onChange={e => setFilterType(e.target.value)}
          className="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-sm text-gray-200"
          aria-label="Filter by type"
        >
          <option value="">All types</option>
          {allTypes.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        {allRegions.length > 0 && (
          <select
            value={filterRegion}
            onChange={e => setFilterRegion(e.target.value)}
            className="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-sm text-gray-200"
            aria-label="Filter by region"
          >
            <option value="">All regions</option>
            {allRegions.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        )}
        <span className="text-xs text-gray-500 ml-auto">{filtered.length} entries</span>
      </div>

      {filtered.length === 0 && (
        <div className="text-center text-gray-500 py-12">
          <div className="text-4xl mb-3">&#127757;</div>
          <p>No world knowledge{entries.length > 0 ? ' matches the filters' : ''}</p>
          {entries.length === 0 && (
            <p className="text-sm mt-1">World entries are created when a story starts from a compiled world.</p>
          )}
        </div>
      )}

      {filtered.map(e => (
        <WorldEntryCard key={e.id} entry={e} onEdit={onEdit} />
      ))}
    </div>
  );
}

function WorldEntryCard({ entry, isActive, stickyLeft = 0, onEdit }) {
  const [expanded, setExpanded] = useState(false);
  const text = entry.text || '';
  const badge = TYPE_COLORS[entry.source_type] || TYPE_COLORS.connection;

  return (
    <div className={`bg-gray-900/60 rounded border p-3 mb-2 transition-colors ${isActive ? ACTIVE_CARD : 'border-gray-700 hover:border-gray-600'}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <span className={`text-[10px] px-1.5 py-0.5 rounded border uppercase tracking-wide ${badge}`}>
              {entry.source_type}
            </span>
            {entry.constant && (
              <span className="text-[10px] px-1.5 py-0.5 rounded border uppercase tracking-wide bg-amber-900/60 text-amber-300 border-amber-800/50" title="Injected into every turn, independent of retrieval">
                always in context
              </span>
            )}
            {stickyLeft > 0 && (
              <span className="text-[10px] px-1.5 py-0.5 rounded border uppercase tracking-wide bg-sky-900/60 text-sky-300 border-sky-800/50" title="Sticky entry: stays in context after being triggered">
                sticky · {stickyLeft} {stickyLeft === 1 ? 'turn' : 'turns'} left
              </span>
            )}
            {entry.region && (
              <span className="text-xs text-gray-500">{entry.region}</span>
            )}
          </div>
          <p
            className="text-sm text-gray-200 leading-relaxed cursor-pointer whitespace-pre-wrap"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? text : text.slice(0, 160) + (text.length > 160 ? '...' : '')}
          </p>
        </div>
        <EditButton onClick={() => onEdit(entry)} label={`Edit world entry ${entry.source_id || entry.id}`} />
      </div>
    </div>
  );
}

// ── RAG debug tab ────────────────────────────────────────────────────────────

function RagDebugTab({ contextQuery }) {
  const [query, setQuery] = useState(contextQuery || '');
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const run = async () => {
    const q = query.trim();
    if (!q || busy) return;
    setBusy(true);
    setError('');
    try {
      setResult(await api.ragDebugQuery(q));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const ragLimit = result?.rag_limit ?? 0;
  const worldRagLimit = result?.world_rag_limit ?? 0;

  return (
    <div>
      <p className="text-xs text-gray-500 mb-2">
        Test what RAG retrieval would surface for a given input: the text is embedded and matched
        against memories and world knowledge exactly like a real turn (turn {result?.turn ?? '—'}).
        Highlighted entries are the ones that would be injected into the storyteller's context.
      </p>
      <div className="flex gap-2 items-start mb-1">
        <textarea
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              run();
            }
          }}
          placeholder="Enter text to test retrieval, e.g. a player input…"
          rows={2}
          className="flex-1 bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200 resize-y"
          aria-label="RAG debug query"
        />
        <button
          onClick={run}
          disabled={busy || !query.trim()}
          className="px-4 py-1.5 bg-purple-700 hover:bg-purple-600 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded text-sm font-medium transition-colors"
        >
          {busy ? 'Searching…' : 'Search'}
        </button>
      </div>
      {contextQuery && (
        <button
          onClick={() => setQuery(contextQuery)}
          className="text-xs text-purple-400 hover:text-purple-300 mb-3"
          title="Use the query from the last real turn"
        >
          Use last turn's query
        </button>
      )}

      {error && <div className="bg-red-900/40 border border-red-700/50 rounded p-3 text-red-200 text-sm mb-3">{error}</div>}

      {result && (
        <div className="space-y-4 mt-2">
          <div>
            <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">
              Memories <span className="normal-case font-normal">(top {ragLimit} injected)</span>
            </div>
            {result.memories.length === 0 && (
              <p className="text-sm text-gray-500">No memories retrieved.</p>
            )}
            {result.memories.map((m, i) => (
              <RagResultCard key={m.id} rank={i + 1} dist={m.dist} injected={i < ragLimit}>
                <div className="flex items-center gap-2 flex-wrap mb-1">
                  {m.turn_range && <span className="text-xs text-gray-500 font-mono">{m.turn_range}</span>}
                  <span className="text-xs text-gray-500" title="Importance">imp {m.importance}/10</span>
                </div>
                <p className="text-sm text-gray-200 leading-relaxed whitespace-pre-wrap">{m.text}</p>
                {(m.entities?.length > 0 || m.topics?.length > 0) && (
                  <div className="flex flex-wrap gap-1 mt-2">
                    {(m.entities || []).map(e => (
                      <span key={e} className="text-[10px] px-1.5 py-0.5 rounded bg-blue-900/40 text-blue-300 border border-blue-800/50">{e}</span>
                    ))}
                    {(m.topics || []).map(t => (
                      <span key={t} className="text-[10px] px-1.5 py-0.5 rounded bg-green-900/40 text-green-300 border border-green-800/50">{t}</span>
                    ))}
                  </div>
                )}
              </RagResultCard>
            ))}
          </div>

          <div>
            <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">
              World Knowledge <span className="normal-case font-normal">(top {worldRagLimit} injected)</span>
            </div>
            {result.world_query !== result.query && (
              <p className="text-xs text-gray-500 mb-2">
                World search used the location-enriched query: <span className="text-gray-400 italic">"{result.world_query}"</span>
              </p>
            )}
            {result.world_entries.length === 0 && (
              <p className="text-sm text-gray-500">No world entries retrieved.</p>
            )}
            {result.world_entries.map((e, i) => (
              <RagResultCard key={e.id} rank={i + 1} dist={e.dist} injected={i < worldRagLimit}>
                <div className="flex items-center gap-2 flex-wrap mb-1">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded border uppercase tracking-wide ${TYPE_COLORS[e.source_type] || TYPE_COLORS.connection}`}>
                    {e.source_type}
                  </span>
                  {e.region && <span className="text-xs text-gray-500">{e.region}</span>}
                </div>
                <p className="text-sm text-gray-200 leading-relaxed whitespace-pre-wrap">{e.text}</p>
              </RagResultCard>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function RagResultCard({ rank, dist, injected, children }) {
  return (
    <div className={`bg-gray-900/60 rounded border p-3 mb-2 transition-colors ${injected ? ACTIVE_CARD : 'border-gray-700 opacity-60'}`}>
      <div className="flex items-start gap-3">
        <div className="shrink-0 text-center w-14">
          <div className="text-lg font-bold text-gray-400">#{rank}</div>
          <div className="text-[10px] text-gray-500 font-mono" title="L2 embedding distance (lower = closer match)">
            {typeof dist === 'number' ? dist.toFixed(3) : '—'}
          </div>
          {injected && (
            <div className="text-[10px] px-1 py-0.5 mt-1 rounded bg-purple-900/40 text-purple-300 border border-purple-800/50">
              injected
            </div>
          )}
        </div>
        <div className="flex-1 min-w-0">{children}</div>
      </div>
    </div>
  );
}

// ── Lorebooks tab ────────────────────────────────────────────────────────────

function LorebooksTab({ saveId, search, setEditing, onEntriesChanged }) {
  const [library, setLibrary] = useState([]);
  const [attached, setAttached] = useState([]);
  const [storyEntries, setStoryEntries] = useState([]); // free-standing, this save only
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [books, setBooks] = useState({});      // id -> full record
  const [openBooks, setOpenBooks] = useState({}); // id -> bool
  const [enabledOnly, setEnabledOnly] = useState(false);
  const [constantOnly, setConstantOnly] = useState(false);

  useEffect(() => {
    if (!saveId) return;
    Promise.all([api.listLorebooks(), api.getSaveLorebooks(saveId)])
      .then(([lib, save]) => {
        setLibrary(lib.lorebooks || []);
        setAttached(save.lorebook_ids || []);
        setStoryEntries(save.story_entries || []);
      })
      .catch(e => setError(e.message));
  }, [saveId]);

  const toggleAttach = async (id, on) => {
    const next = on ? [...attached, id] : attached.filter(a => a !== id);
    setBusy(true);
    setError('');
    try {
      const { lorebook_ids } = await api.setSaveLorebooks(saveId, next);
      setAttached(lorebook_ids);
      onEntriesChanged();
    } catch (e) {
      setError(e.message);
    }
    setBusy(false);
  };

  const toggleOpen = async (id) => {
    const willOpen = !openBooks[id];
    setOpenBooks(prev => ({ ...prev, [id]: willOpen }));
    if (willOpen && !books[id]) {
      try {
        const { lorebook } = await api.getLorebook(id);
        setBooks(prev => ({ ...prev, [id]: lorebook }));
      } catch (e) {
        setError(e.message);
      }
    }
  };

  const toggleEnabled = async (bookId, uid, enabled) => {
    try {
      const { lorebook } = await api.updateLorebookEntry(bookId, uid, { enabled });
      setBooks(prev => ({ ...prev, [bookId]: lorebook }));
      onEntriesChanged();
    } catch (e) {
      setError(e.message);
    }
  };

  // ── free-standing story entries (persisted on the save) ────────────────────

  const STORY_ENTRY_FIELDS = [
    { key: 'title', label: 'Title', type: 'text' },
    { key: 'keys', label: 'Keywords (comma-separated)', type: 'text' },
    { key: 'content', label: 'Content', type: 'textarea', rows: 6 },
    { key: 'enabled', label: 'Enabled', type: 'checkbox' },
    { key: 'constant', label: 'Constant (always in context)', type: 'checkbox' },
    { key: 'sticky_turns', label: 'Sticky turns (stays in context this many turns after triggered; 0 = off)', type: 'number', min: 0 },
  ];

  const splitKeys = (value) => value.split(',').map(k => k.trim()).filter(Boolean);

  const newStoryEntry = () => setEditing({
    title: 'New Story Entry',
    fields: STORY_ENTRY_FIELDS,
    initialValues: { title: '', keys: '', content: '', enabled: true, constant: false, sticky_turns: 0 },
    onSave: async (values) => {
      const { story_entries } = await api.addStoryLorebookEntry(saveId, {
        ...values, keys: splitKeys(values.keys),
      });
      setStoryEntries(story_entries);
      onEntriesChanged();
    },
  });

  const editStoryEntry = (entry) => setEditing({
    title: `Edit Story Entry — ${entry.title || `Entry ${entry.uid}`}`,
    fields: STORY_ENTRY_FIELDS,
    initialValues: {
      title: entry.title || '',
      keys: (entry.keys || []).join(', '),
      content: entry.content || '',
      enabled: !!entry.enabled,
      constant: !!entry.constant,
      sticky_turns: entry.sticky_turns || 0,
    },
    onSave: async (values, changed) => {
      if (Object.keys(changed).length === 0) return;
      const patch = { ...changed };
      if ('keys' in patch) patch.keys = splitKeys(patch.keys);
      const { story_entries } = await api.updateStoryLorebookEntry(saveId, entry.uid, patch);
      setStoryEntries(story_entries);
      onEntriesChanged();
    },
  });

  const toggleStoryEntry = async (uid, enabled) => {
    try {
      const { story_entries } = await api.updateStoryLorebookEntry(saveId, uid, { enabled });
      setStoryEntries(story_entries);
      onEntriesChanged();
    } catch (e) {
      setError(e.message);
    }
  };

  const deleteStoryEntry = async (uid) => {
    try {
      const { story_entries } = await api.deleteStoryLorebookEntry(saveId, uid);
      setStoryEntries(story_entries);
      onEntriesChanged();
    } catch (e) {
      setError(e.message);
    }
  };

  const editEntry = (bookId, entry) => setEditing({
    title: `Edit Lorebook Entry — ${entry.title || `Entry ${entry.uid}`}`,
    fields: [
      { key: 'title', label: 'Title', type: 'text' },
      { key: 'keys', label: 'Keywords (comma-separated)', type: 'text' },
      { key: 'content', label: 'Content', type: 'textarea', rows: 6 },
      { key: 'enabled', label: 'Enabled', type: 'checkbox' },
      { key: 'constant', label: 'Constant (always in context)', type: 'checkbox' },
      { key: 'sticky_turns', label: 'Sticky turns (blank = use the book default)', type: 'text' },
    ],
    initialValues: {
      title: entry.title || '',
      keys: (entry.keys || []).join(', '),
      content: entry.content || '',
      enabled: !!entry.enabled,
      constant: !!entry.constant,
      sticky_turns: entry.sticky_turns ?? '',
    },
    onSave: async (values, changed) => {
      if (Object.keys(changed).length === 0) return;
      const patch = { ...changed };
      if ('keys' in patch) patch.keys = patch.keys.split(',').map(k => k.trim()).filter(Boolean);
      if ('sticky_turns' in patch) {
        // Blank clears the per-entry override (entry inherits the book value).
        const raw = String(patch.sticky_turns).trim();
        patch.sticky_turns = raw === '' ? null : Math.max(0, parseInt(raw, 10) || 0);
      }
      const { lorebook } = await api.updateLorebookEntry(bookId, entry.uid, patch);
      setBooks(prev => ({ ...prev, [bookId]: lorebook }));
      onEntriesChanged();
    },
  });

  const attachedBooks = library.filter(b => attached.includes(b.id));

  const filterEntries = (entries) => {
    let list = entries;
    if (enabledOnly) list = list.filter(e => e.enabled);
    if (constantOnly) list = list.filter(e => e.constant);
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      list = list.filter(e =>
        (e.title || '').toLowerCase().includes(q) ||
        (e.content || '').toLowerCase().includes(q) ||
        (e.keys || []).some(k => k.toLowerCase().includes(q))
      );
    }
    return list;
  };

  const filteredStoryEntries = filterEntries(storyEntries);

  return (
    <div className="space-y-4">
      {error && <div className="bg-red-900/40 border border-red-700/50 rounded p-3 text-red-200 text-sm">{error}</div>}

      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs text-gray-500 font-semibold uppercase tracking-wide">Story Entries</span>
          <button
            onClick={newStoryEntry}
            className="text-xs text-purple-300 hover:text-purple-200 border border-purple-800/50 bg-purple-900/30 rounded px-2 py-1"
            aria-label="Add story entry"
          >
            + New Entry
          </button>
        </div>
        <div className="bg-gray-900/60 rounded border border-gray-700 p-3 space-y-2">
          {storyEntries.length === 0 && (
            <p className="text-sm text-gray-500">
              No story entries yet. Story entries are free-standing lore that belongs to this story only —
              they behave exactly like lorebook entries (keywords, constant, enabled) without needing an imported lorebook.
            </p>
          )}
          {storyEntries.length > 0 && filteredStoryEntries.length === 0 && (
            <p className="text-sm text-gray-500">No story entries match.</p>
          )}
          {filteredStoryEntries.map(entry => (
            <LorebookEntryCard
              key={entry.uid}
              entry={entry}
              onToggle={(enabled) => toggleStoryEntry(entry.uid, enabled)}
              onEdit={() => editStoryEntry(entry)}
              onDelete={() => deleteStoryEntry(entry.uid)}
            />
          ))}
        </div>
      </div>

      <div>
        <div className="text-xs text-gray-500 font-semibold uppercase tracking-wide mb-2">Attached Lorebooks</div>
        <div className="bg-gray-900/60 rounded border border-gray-700 p-3 space-y-1">
          {library.length === 0 && <p className="text-sm text-gray-500">No lorebooks in the library. Import one from the main menu.</p>}
          {library.map(b => (
            <label key={b.id} className={`flex items-center gap-2 text-sm text-gray-300 ${busy ? 'opacity-50' : 'cursor-pointer'}`}>
              <input
                type="checkbox"
                checked={attached.includes(b.id)}
                disabled={busy}
                onChange={e => toggleAttach(b.id, e.target.checked)}
                className="accent-purple-600"
              />
              📚 {b.name}
              <span className="text-xs text-gray-500">({b.enabled_count}/{b.entry_count} entries)</span>
            </label>
          ))}
          <p className="text-[10px] text-gray-600 mt-1">Attached lorebook entries join world knowledge retrieval from the next turn. Edits re-embed and apply from the next turn.</p>
        </div>
      </div>

      {(attachedBooks.length > 0 || storyEntries.length > 0) && (
        <div className="flex items-center gap-4">
          <label className="flex items-center gap-1.5 text-xs text-gray-400 cursor-pointer">
            <input type="checkbox" checked={enabledOnly} onChange={e => setEnabledOnly(e.target.checked)} className="accent-purple-600" />
            Enabled only
          </label>
          <label className="flex items-center gap-1.5 text-xs text-gray-400 cursor-pointer">
            <input type="checkbox" checked={constantOnly} onChange={e => setConstantOnly(e.target.checked)} className="accent-purple-600" />
            Constant only
          </label>
        </div>
      )}

      {attachedBooks.map(b => {
        const record = books[b.id];
        const entries = record ? filterEntries(record.entries || []) : [];
        return (
          <div key={b.id} className="bg-gray-900/40 rounded border border-gray-700">
            <button
              onClick={() => toggleOpen(b.id)}
              className="w-full flex items-center justify-between p-3 text-left text-sm font-medium text-gray-200 hover:bg-gray-800/50 rounded"
            >
              <span>📚 {b.name}</span>
              <span className="text-gray-500 text-xs">{openBooks[b.id] ? '▾' : '▸'}</span>
            </button>
            {openBooks[b.id] && (
              <div className="p-3 pt-0 space-y-2">
                {!record && <p className="text-sm text-gray-500">Loading...</p>}
                {record && entries.length === 0 && <p className="text-sm text-gray-500">No entries match.</p>}
                {entries.map(entry => (
                  <LorebookEntryCard
                    key={entry.uid}
                    entry={entry}
                    bookSticky={record.sticky_turns || 0}
                    onToggle={(enabled) => toggleEnabled(b.id, entry.uid, enabled)}
                    onEdit={() => editEntry(b.id, entry)}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function LorebookEntryCard({ entry, bookSticky = 0, onToggle, onEdit, onDelete }) {
  const [expanded, setExpanded] = useState(false);
  const content = entry.content || '';
  const effectiveSticky = entry.sticky_turns ?? bookSticky;

  return (
    <div className={`p-3 rounded border transition-colors ${
      entry.enabled ? 'border-gray-700 bg-gray-900/60' : 'border-gray-800 bg-gray-900/40 opacity-60'
    }`}>
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-gray-200 text-sm">{entry.title || entry.keys?.[0] || `Entry ${entry.uid}`}</span>
            {entry.constant && (
              <span className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide bg-amber-900/60 text-amber-300 border border-amber-800/50" title="Injected into every turn, independent of retrieval">
                always in context
              </span>
            )}
            {effectiveSticky > 0 && (
              <span
                className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide bg-sky-900/60 text-sky-300 border border-sky-800/50"
                title={entry.sticky_turns != null ? 'Stays in context this many turns after being triggered (per-entry value)' : 'Stays in context this many turns after being triggered (book default)'}
              >
                sticky {effectiveSticky}
              </span>
            )}
          </div>
          {entry.keys?.length > 0 && (
            <p className="text-xs text-gray-500 truncate">🔑 {entry.keys.join(', ')}</p>
          )}
        </div>
        <EditButton onClick={onEdit} label={`Edit lorebook entry ${entry.uid}`} />
        <label className="flex items-center gap-1.5 text-xs text-gray-400 shrink-0 cursor-pointer">
          <input
            type="checkbox"
            checked={!!entry.enabled}
            onChange={e => onToggle(e.target.checked)}
            className="accent-purple-600"
          />
          Enabled
        </label>
        {onDelete && (
          <button
            onClick={(e) => { e.stopPropagation(); onDelete(); }}
            className="text-gray-600 hover:text-red-400 text-sm shrink-0 px-1"
            title="Remove this entry from the story"
            aria-label={`Remove story entry ${entry.uid}`}
          >
            &#128465;
          </button>
        )}
      </div>
      <p
        className={`text-xs text-gray-400 mt-2 whitespace-pre-wrap cursor-pointer ${expanded ? '' : 'line-clamp-3'}`}
        onClick={() => setExpanded(!expanded)}
      >
        {content}
      </p>
    </div>
  );
}

// ── shared bits ──────────────────────────────────────────────────────────────

function TriggeredHeading({ contextQuery }) {
  return (
    <div className="text-xs text-purple-400 font-semibold uppercase tracking-wide mb-2">
      Active This Turn{contextQuery && ` · triggered by "${contextQuery.length > 60 ? contextQuery.slice(0, 60) + '...' : contextQuery}"`}
    </div>
  );
}

function EditButton({ onClick, label }) {
  return (
    <button
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      className="text-gray-600 hover:text-purple-300 text-sm shrink-0 px-1"
      title="Edit"
      aria-label={label}
    >
      &#9998;
    </button>
  );
}

function EditEntryModal({ title, fields, initialValues, onSave, onCancel, onDone }) {
  const [values, setValues] = useState(initialValues);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const setValue = (key, value) => setValues(prev => ({ ...prev, [key]: value }));

  const handleSave = async () => {
    setBusy(true);
    setError('');
    // Only send fields the user actually changed, so the backend re-embeds
    // only when the embedded text itself changed.
    const changed = {};
    for (const f of fields) {
      if (values[f.key] !== initialValues[f.key]) changed[f.key] = values[f.key];
    }
    try {
      await onSave(values, changed);
      onDone();
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="absolute inset-0 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div className="bg-gray-800 border border-gray-600 rounded-lg p-5 shadow-xl w-full max-w-lg max-h-[80vh] overflow-y-auto">
        <h3 className="text-gray-100 font-semibold mb-4">{title}</h3>
        <div className="space-y-3">
          {fields.map(f => (
            <div key={f.key}>
              {f.type === 'checkbox' ? (
                <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={!!values[f.key]}
                    onChange={e => setValue(f.key, e.target.checked)}
                    className="accent-purple-600"
                    disabled={busy}
                  />
                  {f.label}
                </label>
              ) : (
                <>
                  <label className="block text-xs text-gray-400 mb-1">{f.label}</label>
                  {f.type === 'textarea' ? (
                    <textarea
                      value={values[f.key]}
                      onChange={e => setValue(f.key, e.target.value)}
                      rows={f.rows || 4}
                      className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200"
                      disabled={busy}
                    />
                  ) : (
                    <input
                      type={f.type === 'number' ? 'number' : 'text'}
                      value={values[f.key]}
                      min={f.min}
                      max={f.max}
                      onChange={e => setValue(f.key, f.type === 'number' ? Number(e.target.value) : e.target.value)}
                      className="w-full bg-gray-900 border border-gray-600 rounded px-2 py-1.5 text-sm text-gray-200"
                      disabled={busy}
                    />
                  )}
                </>
              )}
            </div>
          ))}
        </div>
        {error && <p className="text-xs text-red-400 mt-3">{error}</p>}
        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onCancel} disabled={busy} className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-white rounded text-sm transition-colors">
            Cancel
          </button>
          <button onClick={handleSave} disabled={busy} className="px-3 py-1.5 bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-white rounded text-sm transition-colors">
            {busy ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>
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
