import { useState, useEffect } from 'react';
import { api } from '../../lib/api';

// Editor for per-module instruction-slot overrides ({mod_id: {slot_id: text}}).
// Rendered by the scenario editor, the story creation screen, and the story
// settings modal. The host owns the value; this component only edits it.
// An empty field means "use the built-in default". `scenarioDefaults`, when
// given, is the linked scenario's override map: reset restores that value
// instead of clearing. `scenarioContext`, when given, is the surrounding
// scenario ({name, scenario_description, starting_prompt, themes, tags,
// pacing}); it rides along with the AI rewrite so the instruction can be made
// aware of the story it belongs to. Modules without instruction slots are
// skipped; modules toggled off are hidden but their entered data stays in the
// host's state (same convention as ModuleTogglePanel).

// Slot lists are static per server run: fetch each module's once and share.
const slotsCache = {};

async function fetchSlots(modId) {
  if (!slotsCache[modId]) {
    slotsCache[modId] = api.getInstructionSlots(modId).then((res) => res.slots || []);
  }
  return slotsCache[modId];
}

function SlotEditor({ modId, slot, text, scenarioDefault, scenarioContext, onText }) {
  const [rewriteReq, setRewriteReq] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const hasScenarioDefault = Boolean((scenarioDefault || '').trim());
  const isCustom = Boolean((text || '').trim());

  const rewrite = async () => {
    const req = rewriteReq.trim();
    if (!req || busy) return;
    setBusy(true);
    setError('');
    try {
      const res = await api.rewriteModuleInstruction(modId, slot.id, {
        request: req,
        currentText: (text || '').trim() || null,
        scenarioContext: scenarioContext || null,
      });
      onText(res.instruction);
      setRewriteReq('');
    } catch (e) {
      setError(e.message || 'Rewrite failed.');
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setError('');
    onText(hasScenarioDefault ? scenarioDefault : '');
  };

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800/50 p-3 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-sm font-medium text-gray-200">{slot.label}</div>
          <div className="text-xs text-gray-500">{slot.description}</div>
        </div>
        <button
          onClick={reset}
          className="shrink-0 px-2 py-1 rounded text-xs border border-gray-700 text-gray-400 hover:bg-gray-700 transition-colors"
          title={hasScenarioDefault
            ? 'Restore the instruction this scenario provides'
            : 'Clear the override and use the built-in instruction'}
        >
          {hasScenarioDefault ? 'Reset to scenario default' : 'Reset to default'}
        </button>
      </div>

      <textarea
        value={text || ''}
        onChange={(e) => { onText(e.target.value); setError(''); }}
        rows={3}
        placeholder="Using the default instruction — type here or describe a change below to customize."
        className="w-full px-3 py-2 rounded-lg bg-gray-900 border border-gray-700 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-purple-500 resize-y"
      />

      <details className="text-xs">
        <summary className="cursor-pointer text-gray-500 hover:text-gray-400">
          View default instruction
        </summary>
        <pre className="mt-1 p-2 rounded bg-gray-900 border border-gray-800 text-gray-400 whitespace-pre-wrap font-sans">
          {slot.default}
        </pre>
      </details>

      <div className="flex items-center gap-2">
        <input
          value={rewriteReq}
          onChange={(e) => setRewriteReq(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); rewrite(); } }}
          placeholder='Describe what you want, e.g. "skills should lean on forbidden pacts"'
          className="flex-1 px-3 py-1.5 rounded-lg bg-gray-900 border border-gray-700 text-xs text-gray-200 placeholder-gray-600 focus:outline-none focus:border-purple-500"
        />
        <button
          onClick={rewrite}
          disabled={busy || !rewriteReq.trim()}
          className="shrink-0 px-3 py-1.5 rounded-lg text-xs bg-purple-700 hover:bg-purple-600 disabled:opacity-50 disabled:cursor-not-allowed text-white transition-colors"
          title={`AI rewrites the ${isCustom ? 'current' : 'default'} instruction to fit your request`}
        >
          {busy ? 'Rewriting…' : '✨ Rewrite'}
        </button>
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  );
}

function ModuleSection({ mod, value, scenarioDefaults, scenarioContext, onChange }) {
  const [slots, setSlots] = useState(null);
  const [loadError, setLoadError] = useState('');

  useEffect(() => {
    let alive = true;
    fetchSlots(mod.id)
      .then((s) => { if (alive) setSlots(s); })
      .catch((e) => { if (alive) setLoadError(e.message || 'Could not load instruction slots.'); });
    return () => { alive = false; };
  }, [mod.id]);

  const modValue = value?.[mod.id] || {};
  const setSlotText = (slotId, text) => {
    const nextMod = { ...modValue, [slotId]: text };
    if (!text) delete nextMod[slotId];
    onChange({ ...(value || {}), [mod.id]: nextMod });
  };

  if (loadError) return <p className="text-xs text-red-400">{mod.name || mod.id}: {loadError}</p>;
  if (!slots) return <p className="text-xs text-gray-500">Loading {mod.name || mod.id} instructions…</p>;

  return (
    <div className="space-y-2">
      {slots.map((slot) => (
        <SlotEditor
          key={slot.id}
          modId={mod.id}
          slot={slot}
          text={modValue[slot.id] || ''}
          scenarioDefault={scenarioDefaults?.[mod.id]?.[slot.id] || ''}
          scenarioContext={scenarioContext}
          onText={(text) => setSlotText(slot.id, text)}
        />
      ))}
    </div>
  );
}

export default function ModuleInstructionsEditor({ modules = [], enabledModules, value, onChange, scenarioDefaults = null, scenarioContext = null }) {
  const isEnabled = (id) => {
    if (!enabledModules) return true;
    return enabledModules instanceof Set ? enabledModules.has(id) : enabledModules.includes(id);
  };
  const withSlots = modules.filter((m) => m.has_instruction_slots && isEnabled(m.id));
  if (withSlots.length === 0) return null;

  return (
    <div className="space-y-3">
      {withSlots.map((mod) => (
        <details key={mod.id} className="rounded-lg border border-gray-700 bg-gray-900/50">
          <summary className="cursor-pointer px-3 py-2 text-sm text-gray-300 hover:text-gray-100 select-none">
            {mod.icon ? `${mod.icon} ` : ''}{mod.name || mod.id} — custom instructions
            {Object.keys(value?.[mod.id] || {}).length > 0 && (
              <span className="ml-2 text-xs text-purple-400">(customized)</span>
            )}
          </summary>
          <div className="p-3 pt-1">
            <ModuleSection mod={mod} value={value} scenarioDefaults={scenarioDefaults} scenarioContext={scenarioContext} onChange={onChange} />
          </div>
        </details>
      ))}
    </div>
  );
}
