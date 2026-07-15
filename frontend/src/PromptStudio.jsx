import { useEffect, useState, useRef } from 'react';
import { api } from './lib/api';

const BLOCK_TYPES = ['static_text', 'engine_context', 'world_context', 'character_context', 'module_prompt', 'chat_history'];
const ROLES = ['system', 'user', 'assistant'];
const PLACEMENTS = ['system_relative', 'chat_injection'];
const CATEGORIES = ['system_prompt', 'post_history', 'narrator', 'world_context', 'character', 'utility', 'other'];
const GENERATION_TYPES = ['storytelling', 'world_building', 'character_creation', 'narration', 'combat', 'memory', 'validation'];

const DEFAULT_BLOCK_IDS = ['core_narrator_rules', 'world_rules_context', 'player_character_context', 'engine_context', 'storyteller_task', 'chat_history'];

const CATEGORY_LABELS = {
  system_prompt: 'System Prompt',
  post_history: 'Post-History',
  narrator: 'Narrator',
  world_context: 'World Context',
  character: 'Character',
  utility: 'Utility',
  other: 'Other',
};

const CATEGORY_COLORS = {
  system_prompt: 'border-l-amber-400',
  post_history: 'border-l-emerald-400',
  narrator: 'border-l-purple-400',
  world_context: 'border-l-blue-400',
  character: 'border-l-pink-400',
  utility: 'border-l-gray-400',
  other: 'border-l-gray-500',
};

function cloneBlocks(blocks) {
  return JSON.parse(JSON.stringify(blocks || []));
}

function blockLabel(value) {
  return String(value || '').replace(/_/g, ' ');
}

function getBlockDescription(block) {
  switch (block.type) {
    case 'engine_context': return 'Auto-generated game state context';
    case 'world_context': return 'World rules & lore from data';
    case 'character_context': return 'Player character identity';
    case 'static_text': return (block.config?.text || '').substring(0, 80) + ((block.config?.text || '').length > 80 ? '...' : '') || 'Static prompt text';
    case 'module_prompt': return 'Module-managed prompt block';
    case 'chat_history': {
      const turns = block.config?.max_turns;
      return turns == null
        ? 'Story chat transcript — all turns'
        : `Story chat transcript — last ${turns} turn${turns === 1 ? '' : 's'}`;
    }
    default: return '';
  }
}

function isDefaultBlock(block) {
  return block.source === 'engine' && DEFAULT_BLOCK_IDS.includes(block.id);
}

function BlockCard({ block, index, draft, dragIndex, onMove, onRemove, onEdit, onSaveAsTemplate, onToggle, onDragStart, onDragOver, onDrop }) {
  const catColor = CATEGORY_COLORS[block.category] || 'border-l-gray-500';
  const displayName = block.display_name || block.id;
  const isDefault = isDefaultBlock(block);
  const isEngine = block.source === 'engine';
  const isOff = block.enabled === false;

  return (
    <div
      className={`relative border border-l-4 rounded-lg p-3 space-y-3 transition-colors ${
        isOff
          ? 'border-l-red-500/70 bg-gray-800/60 border-dashed border-red-900/40'
          : `${catColor} ${isDefault ? 'bg-purple-950/40 border-purple-800/60' : 'bg-gray-950/80 border-gray-700'}`
      }`}
      draggable
      onDragStart={(e) => onDragStart(e, index)}
      onDragOver={onDragOver}
      onDrop={(e) => onDrop(e, index)}
      style={{ opacity: dragIndex === index ? 0.4 : 1 }}
    >
      {isOff && (
        <span className="absolute top-2 right-2 text-[9px] font-bold uppercase tracking-widest text-red-400/40 select-none pointer-events-none rotate-[-4deg]">
          Disabled
        </span>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="cursor-grab text-gray-500 text-xs select-none">{'\u22EE\u22EE'}</span>
          <span className="text-xs font-mono text-gray-500">#{index + 1}</span>
          {isDefault && (
            <span className="text-[10px] px-1.5 py-0.5 bg-purple-700/50 text-purple-300 rounded font-semibold uppercase tracking-wide">Core</span>
          )}
          {isEngine && !isDefault && (
            <span className="text-[10px] px-1.5 py-0.5 bg-blue-900/50 text-blue-300 rounded font-semibold uppercase tracking-wide">Engine</span>
          )}
          {isOff && (
            <span className="text-[10px] px-1.5 py-0.5 bg-red-600 text-white rounded font-bold uppercase tracking-wide flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-white inline-block" />OFF
            </span>
          )}
          <span className={`text-sm font-semibold truncate max-w-[160px] ${isOff ? 'text-gray-500 line-through decoration-red-500/50' : 'text-gray-200'}`} title={block.id}>
            {displayName || block.id}
          </span>
          {displayName && (
            <span className="text-[10px] font-mono text-gray-600 truncate max-w-[100px]" title={block.id}>{block.id}</span>
          )}
          {block.category && (
            <span className="text-[10px] px-1.5 py-0.5 bg-gray-800 text-gray-300 rounded">{CATEGORY_LABELS[block.category] || block.category}</span>
          )}
        </div>
        <div className="flex items-center gap-1 text-xs">
          <button
            onClick={() => onToggle(index, isOff)}
            className={`px-2 py-1 rounded font-semibold transition-colors ${
              isOff
                ? 'bg-red-950/70 text-red-300 hover:bg-red-900'
                : 'bg-emerald-900/50 text-emerald-300 hover:bg-emerald-800'
            }`}
            title={isOff ? 'Block is off — click to enable' : 'Block is on — click to disable'}
          >
            {isOff ? 'OFF' : 'ON'}
          </button>
          <button onClick={() => onMove(index, -1)} disabled={index === 0} className="px-2 py-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded transition-colors">&#8593;</button>
          <button onClick={() => onMove(index, 1)} disabled={index === draft.length - 1} className="px-2 py-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded transition-colors">&#8595;</button>
          <button onClick={() => onEdit(block, index)} className="px-2 py-1 bg-gray-800 hover:bg-gray-700 rounded transition-colors" title="Edit block">&#9998;</button>
          <button onClick={() => onSaveAsTemplate(block)} className="px-2 py-1 bg-gray-800 hover:bg-gray-700 rounded transition-colors" title="Save as template">&#9733;</button>
          <button onClick={() => onRemove(index)} className="px-2 py-1 bg-red-950/70 hover:bg-red-900 text-red-200 rounded transition-colors" title="Remove">&#10005;</button>
        </div>
      </div>

      <div className={`text-xs ${isOff ? 'text-gray-600 italic' : 'text-gray-400'}`}>
        {isOff ? 'Excluded from the compiled prompt — this block is skipped.' : getBlockDescription(block)}
      </div>

      <div className={`grid grid-cols-3 gap-2 text-[10px] ${isOff ? 'text-gray-700' : 'text-gray-600'}`}>
        <span>{blockLabel(block.type)}</span>
        <span>{block.role_type}</span>
        <span>{block.placement === 'chat_injection' ? `injected @ depth ${block.depth ?? 0} · order ${block.order ?? 100}` : 'system top'}</span>
      </div>
    </div>
  );
}

function QuickEditSection({ category, label, draft, defaults, onUpdateBlock, onUpdateConfig, onOpenEditModal, onRestoreBlock, macros, showMacros, setShowMacros }) {
  const block = draft.find((b) => b.category === category);
  const missing = !block;
  const isOff = !missing && block.enabled === false;
  const text = block?.config?.text || '';
  const displayName = block?.display_name || label;

  return (
    <div className={`border rounded-lg p-3 space-y-2 transition-colors ${
      missing ? 'bg-gray-900/70 border-dashed border-gray-700'
      : isOff ? 'bg-gray-950/80 border-dashed border-red-900/40'
      : 'bg-gray-900/70 border-gray-700'
    }`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h4 className={`text-xs font-semibold uppercase tracking-wide ${isOff ? 'text-gray-500 line-through decoration-red-500/50' : 'text-gray-300'}`}>{displayName}</h4>
          {isOff && (
            <span className="text-[10px] px-1.5 py-0.5 bg-red-600 text-white rounded font-bold uppercase tracking-wide flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-white inline-block" />OFF
            </span>
          )}
        </div>
        {!missing && (
          <div className="flex items-center gap-2">
            <button
              onClick={() => onOpenEditModal(block, draft.indexOf(block))}
              className="text-xs px-2 py-0.5 bg-gray-700 hover:bg-gray-600 rounded transition-colors"
              title="Edit full block"
            >
              &#9998; Edit Full
            </button>
            <label className={`flex items-center gap-1 text-xs ${isOff ? 'text-red-300' : 'text-gray-400'}`}>
              <input type="checkbox" checked={block.enabled !== false} onChange={(e) => {
                const idx = draft.indexOf(block);
                if (idx >= 0) onUpdateBlock(idx, { enabled: e.target.checked });
              }} className="accent-purple-500" />
              {isOff ? 'off' : 'on'}
            </label>
          </div>
        )}
      </div>

      {missing ? (
        <div className="flex items-center justify-between">
          <p className="text-xs text-gray-500 italic">This section is not configured.</p>
          <button
            onClick={() => {
              const defBlock = defaults.find((d) => d.category === category);
              if (defBlock) onRestoreBlock(defBlock);
            }}
            className="text-xs px-3 py-1.5 bg-purple-700 hover:bg-purple-600 text-white rounded transition-colors"
          >
            + Restore Default
          </button>
        </div>
      ) : (
        <>
          <textarea
            value={text}
            onChange={(e) => {
              const idx = draft.indexOf(block);
              if (idx >= 0) onUpdateConfig(idx, 'text', e.target.value);
            }}
            rows={4}
            className="w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 font-mono resize-y"
            placeholder="Enter prompt text..."
          />
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowMacros(showMacros === `${category}_quick` ? null : `${category}_quick`)}
              className="text-xs px-2 py-0.5 bg-purple-900/50 hover:bg-purple-800 rounded text-purple-300 transition-colors"
            >
              {showMacros === `${category}_quick` ? 'Hide Macros' : 'Variables'}
            </button>
            {showMacros === `${category}_quick` && <MacroPanel macros={macros} onInsert={() => setShowMacros(null)} />}
          </div>
        </>
      )}
    </div>
  );
}

function MacroPanel({ macros, onInsert }) {
  const handleInsert = (macroKey) => {
    const textarea = document.querySelector('textarea:focus');
    if (textarea) {
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const value = textarea.value;
      const nativeInputValueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
      nativeInputValueSetter.call(textarea, value.substring(0, start) + macroKey + value.substring(end));
      textarea.selectionStart = textarea.selectionEnd = start + macroKey.length;
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      textarea.focus();
    }
    onInsert && onInsert();
  };

  return (
    <div className="bg-gray-800 border border-gray-600 rounded-lg p-2 shadow-lg max-h-40 overflow-y-auto">
      {macros.map((m) => (
        <button
          key={m.key}
          onClick={() => handleInsert(m.key)}
          className="block w-full text-left px-2 py-1 text-xs hover:bg-gray-700 rounded text-gray-300"
          title={m.description}
        >
          <code className="text-purple-300 font-mono">{m.key}</code>
          <span className="text-gray-500 ml-2">{m.description}</span>
        </button>
      ))}
    </div>
  );
}

function EditBlockModal({ editingBlock, macros, showMacros, setShowMacros, onSave, onCancel, onUpdateEditingConfig, onSetEditingBlock }) {
  if (!editingBlock) return null;

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60" role="dialog" aria-modal="true">
      <div className="bg-gray-800 rounded-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto mx-4 shadow-2xl">
        <div className="p-4 border-b border-gray-700 flex justify-between items-center bg-gray-900 rounded-t-xl">
          <h3 className="text-lg font-semibold text-gray-100">Edit Block</h3>
          <button onClick={onCancel} className="text-gray-400 hover:text-white text-xl leading-none">&times;</button>
        </div>
        <div className="p-4 space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <label className="text-xs text-gray-400 space-y-1">
              <span>ID</span>
              <input value={editingBlock.id || ''} onChange={(e) => onSetEditingBlock({ ...editingBlock, id: e.target.value })} className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 font-mono" />
            </label>
            <label className="text-xs text-gray-400 space-y-1">
              <span>Display Name</span>
              <input value={editingBlock.display_name || ''} onChange={(e) => onSetEditingBlock({ ...editingBlock, display_name: e.target.value })} className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100" placeholder="Friendly name..." />
            </label>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            <label className="text-xs text-gray-400 space-y-1">
              <span>Type</span>
              <select value={editingBlock.type || 'static_text'} onChange={(e) => onSetEditingBlock({ ...editingBlock, type: e.target.value, ...(e.target.value === 'chat_history' ? { placement: 'system_relative', depth: null, order: null } : {}) })} className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-2 text-sm text-gray-100">
                {BLOCK_TYPES.map((t) => <option key={t} value={t}>{blockLabel(t)}</option>)}
              </select>
            </label>
            <label className="text-xs text-gray-400 space-y-1">
              <span>Role</span>
              <select value={editingBlock.role_type || 'system'} onChange={(e) => onSetEditingBlock({ ...editingBlock, role_type: e.target.value })} className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-2 text-sm text-gray-100">
                {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </label>
            <label className="text-xs text-gray-400 space-y-1">
              <span>Placement</span>
              <select value={editingBlock.placement || 'system_relative'} disabled={editingBlock.type === 'chat_history'} onChange={(e) => onSetEditingBlock({ ...editingBlock, placement: e.target.value, depth: e.target.value === 'chat_injection' ? (editingBlock.depth ?? 0) : null, order: e.target.value === 'chat_injection' ? (editingBlock.order ?? 100) : null })} className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-2 text-sm text-gray-100 disabled:opacity-40">
                {PLACEMENTS.map((p) => <option key={p} value={p}>{blockLabel(p)}</option>)}
              </select>
            </label>
            <label className="text-xs text-gray-400 space-y-1">
              <span>Depth</span>
              <input type="number" min="0" value={editingBlock.placement === 'chat_injection' ? (editingBlock.depth ?? 0) : ''} disabled={editingBlock.placement !== 'chat_injection'} onChange={(e) => onSetEditingBlock({ ...editingBlock, depth: Math.max(0, parseInt(e.target.value || '0', 10)) })} className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-2 text-sm text-gray-100 disabled:opacity-40" />
            </label>
            <label className="text-xs text-gray-400 space-y-1">
              <span>Order</span>
              <input type="number" value={editingBlock.placement === 'chat_injection' ? (editingBlock.order ?? 100) : ''} disabled={editingBlock.placement !== 'chat_injection'} onChange={(e) => { const parsed = parseInt(e.target.value, 10); onSetEditingBlock({ ...editingBlock, order: Number.isNaN(parsed) ? 100 : parsed }); }} className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-2 text-sm text-gray-100 disabled:opacity-40" />
            </label>
          </div>

          {editingBlock.placement === 'chat_injection' && (
            <p className="text-[11px] text-gray-500">
              Depth counts messages up from the latest player message (0 = the very bottom).
              Order breaks ties between blocks injected at the same depth — lower order
              appears earlier. Default is 100.
            </p>
          )}

          <div className="grid grid-cols-2 gap-3">
            <label className="text-xs text-gray-400 space-y-1">
              <span>Category</span>
              <select value={editingBlock.category || 'other'} onChange={(e) => onSetEditingBlock({ ...editingBlock, category: e.target.value })} className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-2 text-sm text-gray-100">
                {CATEGORIES.map((c) => <option key={c} value={c}>{CATEGORY_LABELS[c] || c}</option>)}
              </select>
            </label>
            <label className="text-xs text-gray-400 space-y-1">
              <span>Enabled</span>
              <label className="flex items-center gap-2 py-2">
                <input type="checkbox" checked={editingBlock.enabled !== false} onChange={(e) => onSetEditingBlock({ ...editingBlock, enabled: e.target.checked })} className="accent-purple-500" />
                <span className="text-sm text-gray-300">{editingBlock.enabled !== false ? 'Active' : 'Disabled'}</span>
              </label>
            </label>
          </div>

          <label className="text-xs text-gray-400 space-y-1">
            <span>Generation Types (empty = all)</span>
            <div className="flex flex-wrap gap-1">
              {GENERATION_TYPES.map((gt) => (
                <label key={gt} className={`text-[10px] px-2 py-1 rounded cursor-pointer transition-colors ${(editingBlock.generation_types || []).includes(gt) ? 'bg-purple-600 text-white' : 'bg-gray-700 text-gray-400 hover:bg-gray-600'}`}>
                  <input
                    type="checkbox"
                    checked={(editingBlock.generation_types || []).includes(gt)}
                    onChange={(e) => {
                      const current = editingBlock.generation_types || [];
                      const next = e.target.checked ? [...current, gt] : current.filter((g) => g !== gt);
                      onSetEditingBlock({ ...editingBlock, generation_types: next.length > 0 ? next : null });
                    }}
                    className="hidden"
                  />
                  {blockLabel(gt)}
                </label>
              ))}
            </div>
          </label>

          {(editingBlock.type === 'static_text' || editingBlock.type === 'module_prompt') && (
            <label className="block text-xs text-gray-400 space-y-1">
              <span>Text</span>
              <textarea
                value={editingBlock.config?.text || ''}
                onChange={(e) => onUpdateEditingConfig('text', e.target.value)}
                rows={12}
                className="w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 font-mono resize-y"
              />
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setShowMacros(showMacros === `edit_${editingBlock.id}` ? null : `edit_${editingBlock.id}`)}
                  className="text-xs px-2 py-0.5 bg-purple-900/50 hover:bg-purple-800 rounded text-purple-300 transition-colors"
                >
                  {showMacros === `edit_${editingBlock.id}` ? 'Hide Variables' : 'Insert Variable'}
                </button>
              </div>
              {showMacros === `edit_${editingBlock.id}` && <MacroPanel macros={macros} onInsert={() => setShowMacros(null)} />}
            </label>
          )}

          {editingBlock.type === 'chat_history' && (
            <label className="block text-xs text-gray-400 space-y-1">
              <span>Turns to Include (empty = all)</span>
              <input
                type="number"
                min="0"
                value={editingBlock.config?.max_turns ?? ''}
                onChange={(e) => {
                  const raw = e.target.value;
                  onUpdateEditingConfig('max_turns', raw === '' ? null : Math.max(0, parseInt(raw, 10) || 0));
                }}
                placeholder="All"
                className="w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100"
              />
              <p className="text-[11px] text-gray-500">
                A turn is one player message plus the replies that follow it. Leave empty to
                send the entire chat transcript; 0 sends no history at all. The block&apos;s
                position in the pipeline decides where the transcript appears.
              </p>
            </label>
          )}

          {editingBlock.type === 'engine_context' && (
            <label className="block text-xs text-gray-400 space-y-1">
              <span>Empty Context Text</span>
              <input value={editingBlock.config?.empty_text || ''} onChange={(e) => onUpdateEditingConfig('empty_text', e.target.value)} className="w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100" />
            </label>
          )}
        </div>
        <div className="p-4 border-t border-gray-700 bg-gray-900 rounded-b-xl flex justify-end gap-2">
          <button onClick={onCancel} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded font-medium transition-colors">Cancel</button>
          <button onClick={onSave} className="px-4 py-2 bg-purple-600 hover:bg-purple-500 text-white rounded font-medium transition-colors">Apply Changes</button>
        </div>
      </div>
    </div>
  );
}

export default function PromptStudio({ isOpen, onClose, modules, promptPipeline, promptTrace, onSave, onPreview, onRefresh, standalone }) {
  const [draft, setDraft] = useState([]);
  const [isSaving, setIsSaving] = useState(false);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState('');
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);
  const [showCloseConfirm, setShowCloseConfirm] = useState(false);
  const [dragIndex, setDragIndex] = useState(null);
  const [activeTab, setActiveTab] = useState('pipeline');
  const [editingBlock, setEditingBlock] = useState(null);
  const [templates, setTemplates] = useState([]);
  const [macros, setMacros] = useState([]);
  const [defaults, setDefaults] = useState([]);
  const [templateCategory, setTemplateCategory] = useState('');
  const [showMacros, setShowMacros] = useState(null);
  const [showAddMenu, setShowAddMenu] = useState(false);
  const [continueText, setContinueText] = useState('');
  const [continueDefault, setContinueDefault] = useState('');
  const [continueSaving, setContinueSaving] = useState(false);
  const [continueDirty, setContinueDirty] = useState(false);
  const importFileRef = useRef(null);
  const stImportRef = useRef(null);
  const addMenuRef = useRef(null);

  const loadTemplates = async () => {
    try {
      const data = await api.getPromptTemplates(templateCategory || undefined);
      setTemplates(data.templates || []);
    } catch { setTemplates([]); }
  };

  const loadMacros = async () => {
    try {
      const data = await api.getPromptMacros();
      setMacros(data.macros || []);
    } catch { setMacros([]); }
  };

  const loadDefaults = async () => {
    try {
      const data = await api.getDefaultBlocks();
      setDefaults(data.defaults || []);
    } catch { setDefaults([]); }
  };

  const loadContinuePrompt = async () => {
    try {
      const data = await api.getContinuePrompt();
      setContinueText(data.text || '');
      setContinueDefault(data.default || '');
      setContinueDirty(false);
    } catch { /* leave as-is */ }
  };

  const handleSaveContinue = async () => {
    setContinueSaving(true);
    setError('');
    try {
      const data = await api.updateContinuePrompt(continueText);
      setContinueText(data.text ?? continueText);
      setContinueDirty(false);
    } catch (err) {
      setError(err.message || 'Failed to save continue prompt.');
    } finally {
      setContinueSaving(false);
    }
  };

  const handleResetContinue = async () => {
    if (!confirm('Reset the continue prompt to its default?')) return;
    setError('');
    try {
      const data = await api.resetContinuePrompt();
      setContinueText(data.text || '');
      setContinueDirty(false);
    } catch (err) {
      setError(err.message || 'Failed to reset continue prompt.');
    }
  };

  useEffect(() => {
    if (isOpen) {
      setDraft(cloneBlocks(promptPipeline));
      setPreview(null);
      setError('');
      setHasUnsavedChanges(false);
      loadTemplates();
      loadMacros();
      loadDefaults();
      loadContinuePrompt();
    }
  }, [isOpen]);

  useEffect(() => { if (isOpen) loadTemplates(); }, [templateCategory]);

  useEffect(() => {
    const handler = (e) => {
      if (addMenuRef.current && !addMenuRef.current.contains(e.target)) {
        setShowAddMenu(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  if (!isOpen) return null;

  const modulePromptBlocks = modules.flatMap((mod) =>
    (mod.prompt_blocks || []).map((block) => ({
      ...block,
      namespacedId: `${mod.id}:${block.id}`,
      moduleId: mod.id,
      moduleName: mod.name || mod.id,
    }))
  );

  const missingDefaults = defaults.filter((def) => !draft.some((b) => b.id === def.id));

  const updateBlock = (index, patch) => {
    setDraft((prev) => prev.map((block, idx) => (idx === index ? { ...block, ...patch } : block)));
    setHasUnsavedChanges(true);
  };

  // Anchor a module's block into the editable stack: the pipeline entry
  // controls position/enabled/placement, the module supplies the content at
  // compile time, and the engine skips it when the module is inactive.
  const insertModuleBlock = (block) => {
    setDraft((prev) => {
      if (prev.some((b) => b.id === block.namespacedId)) return prev;
      return [...prev, {
        id: block.namespacedId,
        type: 'module_prompt',
        source: `module:${block.moduleId}`,
        enabled: true,
        role_type: block.role_type || 'system',
        placement: block.placement || 'system_relative',
        depth: block.placement === 'chat_injection' ? (block.depth ?? 0) : null,
        order: block.placement === 'chat_injection' ? (block.order ?? 100) : null,
        display_name: `${block.moduleName}: ${block.id}`,
        category: 'other',
        generation_types: null,
        config: {},
      }];
    });
    setHasUnsavedChanges(true);
  };

  const updateConfig = (index, key, value) => {
    setDraft((prev) => prev.map((block, idx) => {
      if (idx !== index) return block;
      return { ...block, config: { ...(block.config || {}), [key]: value } };
    }));
    setHasUnsavedChanges(true);
  };

  const updateEditingConfig = (key, value) => {
    setEditingBlock((prev) => prev ? { ...prev, config: { ...(prev.config || {}), [key]: value } } : prev);
  };

  const moveBlock = (index, direction) => {
    const nextIndex = index + direction;
    if (nextIndex < 0 || nextIndex >= draft.length) return;
    setDraft((prev) => {
      const next = [...prev];
      const [block] = next.splice(index, 1);
      next.splice(nextIndex, 0, block);
      return next;
    });
    setHasUnsavedChanges(true);
  };

  const removeBlock = (index) => {
    setDraft((prev) => prev.filter((_, idx) => idx !== index));
    setHasUnsavedChanges(true);
  };

  const restoreBlock = (defaultBlock) => {
    setDraft((prev) => [...prev, cloneBlocks([defaultBlock])[0]]);
    setHasUnsavedChanges(true);
  };

  const addStaticBlock = () => {
    const nextNumber = draft.length + 1;
    setDraft((prev) => [
      ...prev,
      { id: `custom_prompt_${nextNumber}`, type: 'static_text', source: 'user', enabled: true, role_type: 'system', placement: 'system_relative', depth: null, display_name: '', category: 'other', generation_types: null, config: { text: 'New prompt instruction.' } },
    ]);
    setHasUnsavedChanges(true);
    setShowAddMenu(false);
  };

  const addEngineContextBlock = () => {
    const nextNumber = draft.length + 1;
    setDraft((prev) => [
      ...prev,
      { id: `engine_context_${nextNumber}`, type: 'engine_context', source: 'user', enabled: true, role_type: 'system', placement: 'system_relative', depth: null, display_name: 'Engine Context', category: 'utility', generation_types: null, config: { empty_text: 'No additional engine context.' } },
    ]);
    setHasUnsavedChanges(true);
    setShowAddMenu(false);
  };

  const addWorldContextBlock = () => {
    const nextNumber = draft.length + 1;
    setDraft((prev) => [
      ...prev,
      { id: `world_context_${nextNumber}`, type: 'world_context', source: 'user', enabled: true, role_type: 'system', placement: 'system_relative', depth: null, display_name: 'World Context', category: 'world_context', generation_types: null, config: {} },
    ]);
    setHasUnsavedChanges(true);
    setShowAddMenu(false);
  };

  const insertTemplate = async (templateId) => {
    try {
      const data = await api.templateToBlock(templateId);
      setDraft((prev) => [...prev, data.block]);
      setHasUnsavedChanges(true);
    } catch (err) {
      setError(err.message || 'Failed to insert template.');
    }
  };

  const saveAsTemplate = async (block) => {
    const name = prompt('Template name:', block.display_name || block.id);
    if (!name) return;
    try {
      await api.createPromptTemplate(name, { type: block.type, role_type: block.role_type, placement: block.placement, depth: block.depth, order: block.order, config: block.config }, block.category || 'other');
      await loadTemplates();
    } catch (err) {
      setError(err.message || 'Failed to save template.');
    }
  };

  const deleteTemplate = async (templateId) => {
    try {
      await api.deletePromptTemplate(templateId);
      await loadTemplates();
    } catch (err) {
      setError(err.message || 'Failed to delete template.');
    }
  };

  const openEditModal = (block, index) => {
    setEditingBlock({ ...block, _index: index });
  };

  const saveEditModal = () => {
    if (editingBlock._index !== undefined) {
      const { _index, ...rest } = editingBlock;
      setDraft((prev) => prev.map((block, idx) => (idx === _index ? rest : block)));
      setHasUnsavedChanges(true);
    }
    setEditingBlock(null);
  };

  const handleDragStart = (e, index) => {
    setDragIndex(index);
    e.dataTransfer.effectAllowed = 'move';
  };

  const handleDragOver = (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  };

  const handleDrop = (e, index) => {
    e.preventDefault();
    if (dragIndex === null || dragIndex === index) return;
    setDraft((prev) => {
      const next = [...prev];
      const [moved] = next.splice(dragIndex, 1);
      next.splice(index, 0, moved);
      return next;
    });
    setDragIndex(null);
    setHasUnsavedChanges(true);
  };

  const handleSave = async () => {
    setIsSaving(true);
    setError('');
    try {
      await onSave(draft);
      setHasUnsavedChanges(false);
    } catch (err) {
      setError(err.message || 'Failed to save prompt pipeline.');
    } finally {
      setIsSaving(false);
    }
  };

  const handlePreview = async () => {
    setIsPreviewing(true);
    setError('');
    try {
      const data = await onPreview(draft);
      setPreview(data);
    } catch (err) {
      setError(err.message || 'Failed to preview prompt pipeline.');
    } finally {
      setIsPreviewing(false);
    }
  };

  const handleReset = async () => {
    if (!confirm('Reset pipeline to system defaults? All changes will be lost.')) return;
    setError('');
    try {
      let pipeline;
      if (standalone) {
        const data = await api.resetGlobalPromptPipeline();
        pipeline = data.prompt_pipeline || [];
      } else {
        const data = await api.resetPromptPipeline();
        pipeline = data.prompt_pipeline || [];
      }
      await onRefresh();
      setDraft(cloneBlocks(pipeline));
      setHasUnsavedChanges(false);
      setPreview(null);
    } catch (err) {
      setError(err.message || 'Failed to reset pipeline.');
    }
  };

  const handleExport = () => {
    const json = JSON.stringify(draft, null, 2);
    const blob = new Blob([json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'prompt_pipeline_export.json';
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleImport = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (evt) => {
      try {
        const imported = JSON.parse(evt.target.result);
        if (!Array.isArray(imported)) throw new Error('Not a valid pipeline array.');
        if (confirm(`Import ${imported.length} prompt blocks? This replaces the current draft.`)) {
          setDraft(cloneBlocks(imported));
          setHasUnsavedChanges(true);
        }
      } catch (err) {
        setError(`Import failed: ${err.message}`);
      }
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  const handleImportST = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (evt) => {
      try {
        const raw = JSON.parse(evt.target.result);
        const result = await api.importSillyTavernPreset(raw);
        const { blocks, stats } = result;
        const msg = [
          `SillyTavern import complete.`,
          ``,
          `Total entries: ${stats.total}`,
          `Imported as blocks: ${stats.imported}`,
          `Skipped: ${stats.skipped}`,
        ];
        if (stats.skipped_ids.length > 0 && stats.skipped_ids.length <= 10) {
          msg.push(``, `Skipped items:`);
          stats.skipped_ids.forEach((s) => msg.push(`  - ${s.name || s.id}: ${s.reason}`));
        }
        if (blocks.length === 0) {
          setError('No blocks could be imported from this file.');
          return;
        }
        msg.push(``, `Add ${blocks.length} blocks to the current pipeline?`);
        if (confirm(msg.join('\n'))) {
          setDraft((prev) => [...prev, ...cloneBlocks(blocks)]);
          setHasUnsavedChanges(true);
        }
      } catch (err) {
        setError(`ST Import failed: ${err.message}`);
      }
    };
    reader.readAsText(file);
    e.target.value = '';
  };

  const handleClose = () => {
    if (hasUnsavedChanges || continueDirty) {
      setShowCloseConfirm(true);
    } else {
      onClose();
    }
  };

  const activeTrace = preview?.trace || promptTrace || [];
  const orderedTrace = activeTrace
    .filter((entry) => !entry.skipped && entry.message_index !== undefined)
    .slice()
    .sort((a, b) => a.message_index - b.message_index);
  const skippedTrace = activeTrace.filter((entry) => entry.skipped);
  const previewMessages = preview?.messages || [];

  const quickEditProps = { draft, defaults, onUpdateBlock: updateBlock, onUpdateConfig: updateConfig, onOpenEditModal: openEditModal, onRestoreBlock: restoreBlock, macros, showMacros, setShowMacros };

  return (
    <div className={standalone ? "h-full flex flex-col" : "fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4"} role="dialog" aria-modal="true">
      <div className="bg-gray-800 w-full max-w-7xl rounded-lg shadow-2xl border border-gray-700 flex flex-col max-h-[94vh]">
        <div className="p-4 border-b border-gray-700 flex justify-between items-center bg-gray-900 rounded-t-lg">
          <div>
            <h2 className="text-xl font-bold text-gray-100">Prompt Studio</h2>
            <p className="text-xs text-gray-500 mt-1">Edit pipeline, manage prompt templates, insert variables, import/export.</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={handleExport} className="text-xs px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded transition-colors" title="Export pipeline as JSON">Export</button>
            <label className="text-xs px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded transition-colors cursor-pointer">
              Import
              <input ref={importFileRef} type="file" accept=".json" onChange={handleImport} className="hidden" />
            </label>
            <label className="text-xs px-3 py-1.5 bg-emerald-900/50 hover:bg-emerald-800 border border-emerald-700/50 rounded transition-colors cursor-pointer" title="Import a SillyTavern prompt preset (.json)">
              Import ST
              <input ref={stImportRef} type="file" accept=".json" onChange={handleImportST} className="hidden" />
            </label>
            <button onClick={handleReset} className="text-xs px-3 py-1.5 bg-red-950/70 hover:bg-red-900 text-red-200 rounded transition-colors" title="Reset to defaults">Reset</button>
            <button onClick={handleClose} className="text-gray-400 hover:text-white text-2xl leading-none ml-2" aria-label="Close prompt editor">&times;</button>
          </div>
        </div>

        <div className="flex-1 overflow-hidden p-4 grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px] gap-4">
          <div className="overflow-y-auto space-y-4 min-w-0 pr-1">
            <div className="flex items-center gap-2 border-b border-gray-700 pb-2">
              {[
                { key: 'pipeline', label: 'Pipeline Blocks' },
                { key: 'library', label: 'Prompt Library' },
                { key: 'continue', label: 'Continue Prompt' },
              ].map((tab) => (
                <button
                  key={tab.key}
                  onClick={() => setActiveTab(tab.key)}
                  className={`text-sm px-4 py-1.5 rounded transition-colors ${activeTab === tab.key ? 'bg-purple-600 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'}`}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            {activeTab === 'pipeline' && (
              <>
                <section className="space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <h3 className="text-sm font-semibold uppercase tracking-wide text-purple-300">Quick Edit</h3>
                      <p className="text-xs text-gray-500 mt-0.5">Fast access to the most common prompt sections.</p>
                    </div>
                  </div>
                  <QuickEditSection category="system_prompt" label="Main Prompt" {...quickEditProps} />
                  <QuickEditSection category="post_history" label="Post-History Instructions" {...quickEditProps} />
                </section>

                {missingDefaults.length > 0 && (
                  <section className="space-y-2">
                    <div>
                      <h3 className="text-sm font-semibold uppercase tracking-wide text-amber-300">Missing Core Blocks</h3>
                      <p className="text-xs text-gray-500 mt-0.5">These essential pipeline blocks have been removed. Restore them for proper story generation.</p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {missingDefaults.map((def) => (
                        <button
                          key={def.id}
                          onClick={() => restoreBlock(def)}
                          className="text-xs px-3 py-2 bg-amber-950/50 hover:bg-amber-900 border border-amber-700/50 rounded text-amber-300 transition-colors flex items-center gap-2"
                        >
                          + {def.display_name || def.id}
                          <span className="text-amber-500/70 text-[10px] font-mono">{def.id}</span>
                        </button>
                      ))}
                    </div>
                  </section>
                )}

                <section className="space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <h3 className="text-sm font-semibold uppercase tracking-wide text-purple-300">Pipeline Blocks</h3>
                      <p className="text-xs text-gray-500 mt-0.5">Drag to reorder. Chat injections use depth relative to latest player message; order breaks ties at the same depth (lower first).</p>
                    </div>
                    <div className="relative" ref={addMenuRef}>
                      <button
                        onClick={() => setShowAddMenu((v) => !v)}
                        className="px-3 py-2 bg-purple-700 hover:bg-purple-600 rounded text-sm transition-colors"
                      >
                        + Add Block
                      </button>
                      {showAddMenu && (
                        <div className="absolute right-0 mt-1 w-64 bg-gray-800 border border-gray-600 rounded-lg shadow-xl z-30 py-1">
                          <div className="px-3 py-1.5 text-[10px] text-gray-500 uppercase tracking-wide">Add New Block</div>
                          <button onClick={addStaticBlock} className="block w-full text-left px-3 py-1.5 text-xs text-gray-200 hover:bg-gray-700 transition-colors">Static Text Block</button>
                          <button onClick={addEngineContextBlock} className="block w-full text-left px-3 py-1.5 text-xs text-gray-200 hover:bg-gray-700 transition-colors">Engine Context Block</button>
                          <button onClick={addWorldContextBlock} className="block w-full text-left px-3 py-1.5 text-xs text-gray-200 hover:bg-gray-700 transition-colors">World Context Block</button>
                          {missingDefaults.length > 0 && (
                            <>
                              <div className="px-3 py-1.5 text-[10px] text-gray-500 uppercase tracking-wide border-t border-gray-700 mt-1">Restore Core Blocks</div>
                              {missingDefaults.map((def) => (
                                <button
                                  key={def.id}
                                  onClick={() => { restoreBlock(def); setShowAddMenu(false); }}
                                  className="block w-full text-left px-3 py-1.5 text-xs text-amber-300 hover:bg-gray-700 transition-colors"
                                >
                                  Restore {def.display_name || def.id}
                                </button>
                              ))}
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  </div>

                  {draft.map((block, index) => (
                    <BlockCard
                      key={`${block.id}-${index}`}
                      block={block}
                      index={index}
                      draft={draft}
                      dragIndex={dragIndex}
                      onMove={moveBlock}
                      onRemove={removeBlock}
                      onEdit={openEditModal}
                      onSaveAsTemplate={saveAsTemplate}
                      onToggle={(idx, currentlyOff) => updateBlock(idx, { enabled: currentlyOff })}
                      onDragStart={handleDragStart}
                      onDragOver={handleDragOver}
                      onDrop={handleDrop}
                    />
                  ))}

                  {draft.length === 0 && (
                    <div className="border border-dashed border-gray-700 rounded-lg p-8 text-center text-gray-500">No save-owned prompt blocks. Add a static block or insert from the Prompt Library.</div>
                  )}
                </section>
              </>
            )}

            {activeTab === 'library' && (
              <section className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold uppercase tracking-wide text-purple-300">Prompt Library</h3>
                    <p className="text-xs text-gray-500 mt-0.5">Reusable prompt templates. Click to insert, save blocks as templates from the pipeline.</p>
                  </div>
                </div>

                <div className="flex flex-wrap gap-1">
                  <button
                    onClick={() => setTemplateCategory('')}
                    className={`text-xs px-2 py-1 rounded transition-colors ${!templateCategory ? 'bg-purple-600 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'}`}
                  >
                    All
                  </button>
                  {CATEGORIES.map((cat) => (
                    <button
                      key={cat}
                      onClick={() => setTemplateCategory(cat)}
                      className={`text-xs px-2 py-1 rounded transition-colors ${templateCategory === cat ? 'bg-purple-600 text-white' : 'bg-gray-700 text-gray-300 hover:bg-gray-600'}`}
                    >
                      {CATEGORY_LABELS[cat] || cat}
                    </button>
                  ))}
                </div>

                <div className="space-y-2">
                  {templates.map((tpl) => (
                    <div key={tpl.id} className="bg-gray-900/70 border border-gray-700 rounded-lg p-3 flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-sm font-semibold text-gray-200">{tpl.name}</span>
                          <span className="text-[10px] font-mono text-gray-600">{tpl.id}</span>
                          <span className={`text-[10px] px-1 py-0.5 rounded ${(CATEGORY_COLORS[tpl.category] || 'border-l-gray-500').replace('border-l-', 'bg-').replace('-400', '-900/50')} text-gray-400`}>
                            {CATEGORY_LABELS[tpl.category] || tpl.category}
                          </span>
                        </div>
                        <div className="grid grid-cols-3 gap-2 mt-1 text-[11px] text-gray-500">
                          <span>{blockLabel(tpl.type)}</span>
                          <span>{tpl.role_type}</span>
                          <span>{blockLabel(tpl.placement)}</span>
                        </div>
                        {tpl.config?.text && (
                          <div className="text-xs text-gray-500 mt-1 font-mono truncate max-h-6">
                            {tpl.config.text.substring(0, 100)}{tpl.config.text.length > 100 ? '...' : ''}
                          </div>
                        )}
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        <button
                          onClick={() => insertTemplate(tpl.id)}
                          className="text-xs px-2 py-1 bg-purple-700 hover:bg-purple-600 rounded transition-colors"
                        >
                          Insert
                        </button>
                        <button
                          onClick={() => deleteTemplate(tpl.id)}
                          className="text-xs px-2 py-1 bg-red-950/70 hover:bg-red-900 text-red-200 rounded transition-colors"
                        >
                          Del
                        </button>
                      </div>
                    </div>
                  ))}
                  {templates.length === 0 && (
                    <div className="border border-dashed border-gray-700 rounded-lg p-8 text-center text-gray-500 italic">
                      No saved templates yet. Save pipeline blocks as templates using the &#9733; button.
                    </div>
                  )}
                </div>
              </section>
            )}

            {activeTab === 'continue' && (
              <section className="space-y-3">
                <div>
                  <h3 className="text-sm font-semibold uppercase tracking-wide text-purple-300">Continue Prompt</h3>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Sent as the player turn when you press Send with an empty box. The normal
                    context is compiled without a player message, and this instruction is
                    appended so the story advances on its own. Supports the same variables as
                    pipeline blocks.
                  </p>
                </div>

                <textarea
                  value={continueText}
                  onChange={(e) => { setContinueText(e.target.value); setContinueDirty(true); }}
                  rows={8}
                  className="w-full bg-gray-950 border border-gray-700 rounded px-3 py-2 text-sm text-gray-100 font-mono resize-y"
                  placeholder="Continue the story from where it left off..."
                />

                <div className="flex items-center gap-2 flex-wrap">
                  <button
                    onClick={() => setShowMacros(showMacros === 'continue' ? null : 'continue')}
                    className="text-xs px-2 py-0.5 bg-purple-900/50 hover:bg-purple-800 rounded text-purple-300 transition-colors"
                  >
                    {showMacros === 'continue' ? 'Hide Variables' : 'Insert Variable'}
                  </button>
                  <div className="flex-1" />
                  <button
                    onClick={handleResetContinue}
                    className="text-xs px-3 py-1.5 bg-red-950/70 hover:bg-red-900 text-red-200 rounded transition-colors"
                  >
                    Reset to Default
                  </button>
                  <button
                    onClick={handleSaveContinue}
                    disabled={continueSaving || !continueDirty}
                    className="text-xs px-3 py-1.5 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white rounded font-medium transition-colors"
                  >
                    {continueSaving ? 'Saving...' : 'Save Continue Prompt'}
                  </button>
                </div>
                {showMacros === 'continue' && <MacroPanel macros={macros} onInsert={() => setShowMacros(null)} />}

                {continueDefault && continueText.trim() !== continueDefault.trim() && (
                  <p className="text-[11px] text-gray-600">
                    Customized — differs from the built-in default.
                  </p>
                )}
              </section>
            )}
          </div>

          <aside className="space-y-4 min-w-0 overflow-y-auto pr-1">
            {!standalone && (
            <section className="bg-gray-900/70 border border-gray-700 rounded-lg p-4">
              <div className="flex items-center justify-between gap-3 mb-3">
                <div>
                  <h3 className="text-sm font-semibold uppercase tracking-wide text-purple-300">Compiled Preview</h3>
                  <p className="text-xs text-gray-500 mt-1">Draft pipeline + active state + module blocks.</p>
                </div>
                <button onClick={handlePreview} disabled={isPreviewing} className="text-xs px-2 py-1 bg-purple-700 hover:bg-purple-600 disabled:opacity-50 rounded transition-colors">{isPreviewing ? '...' : 'Preview'}</button>
              </div>
              <div className="space-y-3 max-h-96 overflow-y-auto pr-1">
                {previewMessages.map((message, index) => (
                  <div key={`${message.role}-${index}`} className="bg-gray-800/70 border border-gray-700 rounded p-3 text-xs">
                    <div className="flex justify-between gap-2 mb-2">
                      <span className="font-semibold text-gray-200">Msg #{index}</span>
                      <span className="text-purple-300 font-mono">{message.role}</span>
                    </div>
                    <pre className="whitespace-pre-wrap break-words text-gray-300 font-mono text-[11px] leading-relaxed max-h-56 overflow-y-auto">{message.content}</pre>
                  </div>
                ))}
                {previewMessages.length === 0 && (
                  <div className="text-sm text-gray-500 italic">Click Preview to compile the current draft without saving.</div>
                )}
              </div>
            </section>
            )}

            <section className="bg-gray-900/70 border border-gray-700 rounded-lg p-4">
              <h3 className="text-sm font-semibold uppercase tracking-wide text-purple-300 mb-3">Module Blocks</h3>
              <p className="text-xs text-gray-500 mb-3">
                Prompt blocks contributed by active modules (e.g. the Plot Director's thread block),
                rendered fresh each turn from module state. Insert one into the pipeline to control
                its position and toggle it like any other block — it is skipped automatically when
                the module isn't active. Uninserted blocks are appended after the pipeline.
              </p>
              <div className="space-y-3 max-h-64 overflow-y-auto pr-1">
                {modulePromptBlocks.map((block) => {
                  const inDraft = draft.some((b) => b.id === block.namespacedId);
                  return (
                    <div key={block.namespacedId} className="bg-gray-800/70 border border-gray-700 rounded p-3 text-xs">
                      <div className="font-mono text-gray-100 break-all">{block.namespacedId}</div>
                      <div className="text-gray-500 mt-1">{block.moduleName}</div>
                      <div className="grid grid-cols-2 gap-2 mt-2 text-gray-400">
                        <span>{blockLabel(block.type)}</span>
                        <span>{blockLabel(block.placement)}</span>
                        <span>{block.role_type}</span>
                        <span>{block.placement === 'chat_injection' ? `depth ${block.depth ?? 0} · order ${block.order ?? 100}` : 'depth n/a'}</span>
                      </div>
                      <button
                        onClick={() => insertModuleBlock(block)}
                        disabled={inDraft}
                        className="mt-2 w-full text-[11px] px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-40 disabled:cursor-not-allowed text-gray-200 transition-colors"
                      >
                        {inDraft ? '✓ In pipeline' : '+ Insert into pipeline'}
                      </button>
                    </div>
                  );
                })}
                {modulePromptBlocks.length === 0 && <div className="text-sm text-gray-500 italic">No module prompt blocks loaded.</div>}
              </div>
            </section>

            {!standalone && (
            <section className="bg-gray-900/70 border border-gray-700 rounded-lg p-4">
              <div className="flex items-center justify-between gap-3 mb-3">
                <h3 className="text-sm font-semibold uppercase tracking-wide text-purple-300">{preview ? 'Preview Trace' : 'Last Trace'}</h3>
                <button onClick={onRefresh} className="text-xs px-2 py-1 bg-gray-800 hover:bg-gray-700 rounded transition-colors">Refresh</button>
              </div>
              <div className="space-y-2 max-h-80 overflow-y-auto pr-1">
                {orderedTrace.map((entry) => (
                  <div key={`${entry.id}-${entry.message_index}`} className="bg-gray-800/70 border border-gray-700 rounded p-3 text-xs">
                    <div className="flex justify-between gap-2">
                      <span className="font-mono text-gray-100 break-all">{entry.id}</span>
                      <span className="text-purple-300">#{entry.message_index}</span>
                    </div>
                    <div className="text-gray-500 mt-1">{entry.role_type} / {blockLabel(entry.placement)} / {entry.source}</div>
                  </div>
                ))}
                {orderedTrace.length === 0 && <div className="text-sm text-gray-500 italic">No prompt trace yet.</div>}
              </div>
              {skippedTrace.length > 0 && (
                <div className="mt-4 pt-4 border-t border-gray-700">
                  <h4 className="text-xs font-semibold text-gray-400 mb-2">Skipped</h4>
                  <div className="space-y-1 text-xs text-gray-500">
                    {skippedTrace.map((entry) => (
                      <div key={`${entry.id}-${entry.reason}`} className="font-mono break-all">{entry.id}: {entry.reason}</div>
                    ))}
                  </div>
                </div>
              )}
            </section>
            )}
          </aside>
        </div>

        <div className="p-4 border-t border-gray-700 bg-gray-900 rounded-b-lg flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div className="text-sm text-red-300 min-h-5">{error}</div>
          <div className="flex justify-end gap-2">
            <button onClick={handleClose} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded font-medium transition-colors">Close</button>
            <button onClick={handlePreview} disabled={isPreviewing} className="px-4 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white rounded font-medium transition-colors">{isPreviewing ? '...' : 'Preview'}</button>
            <button onClick={handleSave} disabled={isSaving} className="px-4 py-2 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white rounded font-medium transition-colors">{isSaving ? 'Saving...' : 'Save Pipeline'}</button>
          </div>
        </div>
      </div>

      <EditBlockModal
        editingBlock={editingBlock}
        macros={macros}
        showMacros={showMacros}
        setShowMacros={setShowMacros}
        onSave={saveEditModal}
        onCancel={() => setEditingBlock(null)}
        onUpdateEditingConfig={updateEditingConfig}
        onSetEditingBlock={setEditingBlock}
      />

      {showCloseConfirm && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60" role="dialog" aria-modal="true">
          <div className="bg-gray-800 rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl">
            <h3 className="text-lg font-semibold mb-2">Unsaved Changes</h3>
            <p className="text-gray-300 text-sm mb-6">You have unsaved changes to the prompt pipeline. Discard them?</p>
            <div className="flex justify-end gap-3">
              <button onClick={() => setShowCloseConfirm(false)} className="px-4 py-2 text-sm rounded-lg bg-gray-700 hover:bg-gray-600 transition-colors">Keep Editing</button>
              <button onClick={() => { setShowCloseConfirm(false); onClose(); }} className="px-4 py-2 text-sm rounded-lg bg-red-600 hover:bg-red-500 text-white transition-colors">Discard</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
