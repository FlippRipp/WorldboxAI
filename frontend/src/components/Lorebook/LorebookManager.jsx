import { useState, useEffect, useRef } from 'react';
import { api } from '../../lib/api';
import EditEntryModal, {
  LOREBOOK_ENTRY_FIELDS,
  lorebookEntryInitialValues,
  lorebookEntryPatch,
} from '../EditEntryModal';

// Library of imported SillyTavern lorebooks (World Info). Entries are embedded
// into a story's RAG index when the book is linked to the story's scenario or
// world (or attached to the save directly). This manager handles importing,
// browsing/toggling entries, and editing scenario/world links.
function entryMatches(entry, q) {
  return (entry.title || '').toLowerCase().includes(q) ||
    (entry.content || '').toLowerCase().includes(q) ||
    (entry.keys || []).some(k => k.toLowerCase().includes(q)) ||
    (entry.secondary_keys || []).some(k => k.toLowerCase().includes(q));
}

export default function LorebookManager({ onBack }) {
  const [lorebooks, setLorebooks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null); // full lorebook record
  const [importing, setImporting] = useState(false);
  const [search, setSearch] = useState('');
  const [fullBooks, setFullBooks] = useState({}); // id -> record, for deep search in the list view
  const [editing, setEditing] = useState(null); // {title, fields, initialValues, onSave}
  const fileInputRef = useRef(null);

  const refresh = () => {
    setLoading(true);
    api.listLorebooks()
      .then((d) => setLorebooks(d.lorebooks || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(refresh, []);

  const q = search.trim().toLowerCase();

  // Searching in the list view also matches entry titles/keywords/content, so
  // full records are fetched lazily the first time a query is typed.
  useEffect(() => {
    if (!q || selected) return;
    const missing = lorebooks.filter((b) => !fullBooks[b.id]);
    if (missing.length === 0) return;
    let cancelled = false;
    Promise.all(missing.map((b) =>
      api.getLorebook(b.id).then((d) => [b.id, d.lorebook]).catch(() => null)
    )).then((pairs) => {
      if (!cancelled) {
        setFullBooks((prev) => ({ ...prev, ...Object.fromEntries(pairs.filter(Boolean)) }));
      }
    });
    return () => { cancelled = true; };
  }, [q, selected, lorebooks]); // eslint-disable-line react-hooks/exhaustive-deps

  const entryMatchCount = (bookId) => {
    const record = fullBooks[bookId];
    return record ? (record.entries || []).filter((e) => entryMatches(e, q)).length : 0;
  };

  const visibleBooks = !q ? lorebooks : lorebooks.filter((b) =>
    b.name.toLowerCase().includes(q) ||
    (b.description || '').toLowerCase().includes(q) ||
    entryMatchCount(b.id) > 0
  );

  const visibleEntries = selected
    ? (q ? selected.entries.filter((e) => entryMatches(e, q)) : selected.entries)
    : [];

  const handleImportFile = (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      setImporting(true);
      try {
        const raw = JSON.parse(reader.result);
        const fallbackName = file.name.replace(/\.json$/i, '');
        const { lorebook, stats } = await api.importLorebook(raw, raw.name ? null : fallbackName);
        refresh();
        alert(`Imported "${lorebook.name}": ${stats.imported} entries (${stats.skipped} skipped).`);
      } catch (err) {
        alert(`Import failed: ${err.message}`);
      }
      setImporting(false);
    };
    reader.readAsText(file);
  };

  const cacheBook = (lorebook) => {
    setSelected(lorebook);
    setFullBooks((prev) => ({ ...prev, [lorebook.id]: lorebook }));
  };

  const openDetail = async (id) => {
    try {
      const { lorebook } = await api.getLorebook(id);
      cacheBook(lorebook);
    } catch (e) {
      alert(`Failed to load lorebook: ${e.message}`);
    }
  };

  const handleDelete = async (id) => {
    if (!window.confirm('Delete this lorebook? Stories re-sync on next load and lose its entries.')) return;
    try {
      await api.deleteLorebook(id);
      setFullBooks((prev) => { const next = { ...prev }; delete next[id]; return next; });
      refresh();
    } catch (e) {
      alert(`Failed to delete: ${e.message}`);
    }
  };

  const toggleEntry = async (uid, enabled) => {
    try {
      const { lorebook } = await api.setLorebookEntryEnabled(selected.id, uid, enabled);
      cacheBook(lorebook);
    } catch (e) {
      alert(`Failed to update entry: ${e.message}`);
    }
  };

  const editEntry = (entry) => setEditing({
    title: `Edit Entry — ${entry.title || `Entry ${entry.uid}`}`,
    fields: LOREBOOK_ENTRY_FIELDS,
    initialValues: lorebookEntryInitialValues(entry),
    onSave: async (values, changed) => {
      if (Object.keys(changed).length === 0) return;
      const { lorebook } = await api.updateLorebookEntry(
        selected.id, entry.uid, lorebookEntryPatch(changed));
      cacheBook(lorebook);
    },
  });

  const saveStickyTurns = async (value) => {
    const sticky = Math.max(0, parseInt(value, 10) || 0);
    if (sticky === (selected.sticky_turns || 0)) return;
    try {
      const { lorebook } = await api.updateLorebook(selected.id, { sticky_turns: sticky });
      cacheBook(lorebook);
    } catch (e) {
      alert(`Failed to update lorebook: ${e.message}`);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center p-6">
      <div className="w-full max-w-3xl">
        <button
          onClick={selected ? () => setSelected(null) : onBack}
          className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-8"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          {selected ? 'Back to Lorebooks' : 'Back to Menu'}
        </button>

        {!selected ? (
          <>
            <div className="flex items-center justify-between mb-6">
              <div>
                <h2 className="text-3xl font-bold text-gray-100 mb-2">Lorebooks</h2>
                <p className="text-gray-500 text-sm">
                  Import SillyTavern World Info. Linked entries are retrieved by relevance during play; constant entries are always in context.
                </p>
              </div>
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={importing}
                className="px-4 py-2 rounded-lg bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-sm font-medium transition-colors whitespace-nowrap"
              >
                {importing ? 'Importing...' : '+ Import JSON'}
              </button>
              <input ref={fileInputRef} type="file" accept=".json,application/json" onChange={handleImportFile} className="hidden" />
            </div>

            {lorebooks.length > 0 && (
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search books and entries (title, keywords, content)…"
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 mb-4"
                aria-label="Search lorebooks"
              />
            )}

            {loading ? (
              <div className="text-gray-500 text-center py-12">Loading...</div>
            ) : lorebooks.length === 0 ? (
              <p className="text-gray-500 text-center py-12 border border-dashed border-gray-700 rounded-lg">
                No lorebooks yet. Import a SillyTavern World Info JSON to add lore to your stories.
              </p>
            ) : visibleBooks.length === 0 ? (
              <p className="text-gray-500 text-center py-12 border border-dashed border-gray-700 rounded-lg">
                No lorebooks or entries match "{search.trim()}".
              </p>
            ) : (
              <div className="space-y-2">
                {visibleBooks.map((b) => (
                  <div key={b.id} className="flex items-center justify-between p-4 rounded-lg border border-gray-700 bg-gray-800/50">
                    <div className="flex items-center gap-3">
                      <span className="text-xl">📚</span>
                      <div>
                        <h4 className="font-medium text-gray-200">{b.name}</h4>
                        <p className="text-xs text-gray-500">
                          {b.enabled_count}/{b.entry_count} entries enabled
                          {b.constant_count > 0 ? ` · ${b.constant_count} constant` : ''}
                          {q && entryMatchCount(b.id) > 0 && (
                            <span className="text-purple-400"> · {entryMatchCount(b.id)} matching {entryMatchCount(b.id) === 1 ? 'entry' : 'entries'}</span>
                          )}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <button onClick={() => openDetail(b.id)} className="px-4 py-1.5 rounded-lg bg-gray-700 hover:bg-gray-600 text-sm transition-colors">Open</button>
                      <button onClick={() => handleDelete(b.id)} className="px-3 py-1.5 rounded-lg bg-red-900/50 hover:bg-red-800 border border-red-800/50 text-sm text-red-300 transition-colors" title="Delete">
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        ) : (
          <>
            <h2 className="text-3xl font-bold text-gray-100 mb-1">{selected.name}</h2>
            {selected.description && <p className="text-gray-500 text-sm mb-4">{selected.description}</p>}

            <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30 mb-4 flex items-center gap-3 flex-wrap">
              <label htmlFor="lorebook-sticky-turns" className="text-sm font-medium text-gray-300">Sticky turns</label>
              <input
                id="lorebook-sticky-turns"
                key={selected.id}
                type="number"
                min="0"
                defaultValue={selected.sticky_turns || 0}
                onBlur={(e) => saveStickyTurns(e.target.value)}
                className="w-20 bg-gray-900 border border-gray-600 rounded px-2 py-1 text-sm text-gray-200"
              />
              <p className="text-xs text-gray-500 flex-1 min-w-[200px]">
                Once triggered by retrieval, entries stay in context for this many extra turns (0 = off).
                Individual entries can override this from a story's Memory browser.
              </p>
            </div>

            <LorebookLinkEditor lorebookId={selected.id} />

            <div className="flex items-center justify-between gap-3 mt-6 mb-3 flex-wrap">
              <h3 className="text-lg font-semibold text-gray-200">
                Entries{' '}
                <span className="text-sm font-normal text-gray-500">
                  ({q ? `${visibleEntries.length} of ${selected.entries.length}` : selected.entries.length})
                </span>
              </h3>
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search entries…"
                className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 w-64"
                aria-label="Search entries"
              />
            </div>
            {q && visibleEntries.length === 0 && (
              <p className="text-gray-500 text-center py-8 border border-dashed border-gray-700 rounded-lg">
                No entries match "{search.trim()}".
              </p>
            )}
            <div className="space-y-2">
              {visibleEntries.map((entry) => (
                <div
                  key={entry.uid}
                  className={`p-3 rounded-lg border ${entry.enabled ? 'border-gray-700 bg-gray-800/50' : 'border-gray-800 bg-gray-900/50 opacity-60'}`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium text-gray-200 text-sm">{entry.title || entry.keys[0] || `Entry ${entry.uid}`}</span>
                        {entry.constant && (
                          <span className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide bg-amber-900/60 text-amber-300 border border-amber-800/50">constant</span>
                        )}
                        {(entry.sticky_turns ?? selected.sticky_turns ?? 0) > 0 && (
                          <span
                            className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide bg-sky-900/60 text-sky-300 border border-sky-800/50"
                            title={entry.sticky_turns != null ? 'Per-entry sticky override' : 'Inherited from the book setting'}
                          >
                            sticky {entry.sticky_turns ?? selected.sticky_turns}
                          </span>
                        )}
                        {entry.injection_depth != null && (
                          <span
                            className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide bg-emerald-900/60 text-emerald-300 border border-emerald-800/50"
                            title="When active, injected into the chat this many messages from the bottom instead of the lore block"
                          >
                            @ depth {entry.injection_depth}
                          </span>
                        )}
                      </div>
                      {entry.keys.length > 0 && (
                        <p className="text-xs text-gray-500 truncate">🔑 {entry.keys.join(', ')}</p>
                      )}
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <button
                        onClick={() => editEntry(entry)}
                        className="text-gray-600 hover:text-purple-300 text-sm px-1"
                        title="Edit entry"
                        aria-label={`Edit entry ${entry.uid}`}
                      >
                        &#9998;
                      </button>
                      <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={entry.enabled}
                          onChange={(e) => toggleEntry(entry.uid, e.target.checked)}
                          className="accent-purple-600"
                        />
                        Enabled
                      </label>
                    </div>
                  </div>
                  <p className="text-xs text-gray-400 mt-2 whitespace-pre-wrap line-clamp-3">{entry.content}</p>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

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

// Checkbox lists linking this lorebook to scenarios and (when the worldgen
// module is available) worlds. New stories created from a linked source
// inherit the lorebook automatically.
function LorebookLinkEditor({ lorebookId }) {
  const [scenarios, setScenarios] = useState([]);
  const [worlds, setWorlds] = useState(null); // null = module unavailable
  const [links, setLinks] = useState({}); // {"scenario:<id>": bool, "world:<id>": bool}

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const linkState = {};
      try {
        const { scenarios: sc } = await api.listScenarios();
        if (cancelled) return;
        setScenarios(sc || []);
        for (const s of sc || []) {
          const { lorebook_ids } = await api.getLorebookLinks('scenario', s.id);
          linkState[`scenario:${s.id}`] = lorebook_ids.includes(lorebookId);
        }
      } catch { /* scenarios always exist; ignore */ }
      try {
        const { worlds: w } = await api.listWorlds();
        if (cancelled) return;
        setWorlds(w || []);
        for (const world of w || []) {
          const { lorebook_ids } = await api.getLorebookLinks('world', world.id);
          linkState[`world:${world.id}`] = lorebook_ids.includes(lorebookId);
        }
      } catch {
        if (!cancelled) setWorlds(null); // worldgen module off — hide the section
      }
      if (!cancelled) setLinks(linkState);
    })();
    return () => { cancelled = true; };
  }, [lorebookId]);

  const toggleLink = async (kind, targetId, linked) => {
    try {
      const { lorebook_ids } = await api.getLorebookLinks(kind, targetId);
      const next = linked
        ? [...new Set([...lorebook_ids, lorebookId])]
        : lorebook_ids.filter((id) => id !== lorebookId);
      await api.setLorebookLinks(kind, targetId, next);
      setLinks((prev) => ({ ...prev, [`${kind}:${targetId}`]: linked }));
    } catch (e) {
      alert(`Failed to update link: ${e.message}`);
    }
  };

  const section = (title, kind, items) => (
    <div>
      <h4 className="text-sm font-medium text-gray-300 mb-2">{title}</h4>
      {items.length === 0 ? (
        <p className="text-xs text-gray-500">None yet.</p>
      ) : (
        <div className="space-y-1">
          {items.map((item) => (
            <label key={item.id} className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
              <input
                type="checkbox"
                checked={!!links[`${kind}:${item.id}`]}
                onChange={(e) => toggleLink(kind, item.id, e.target.checked)}
                className="accent-purple-600"
              />
              {item.name || item.id}
            </label>
          ))}
        </div>
      )}
    </div>
  );

  return (
    <div className="p-4 rounded-lg border border-gray-700 bg-gray-800/30">
      <p className="text-xs text-gray-500 mb-3">
        New stories created from a linked scenario or world include this lorebook automatically.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {section('Scenarios', 'scenario', scenarios)}
        {worlds !== null && section('Worlds', 'world', worlds)}
      </div>
    </div>
  );
}
