import { useState, useEffect, useRef, useCallback } from 'react';
import { api } from 'api';
import { storage } from 'storage';
import AgentBuildObserver from './AgentBuildObserver';

function AutoTextarea({ value, onChange, disabled, minRows = 3, placeholder }) {
  const ref = useRef(null);

  const adjustHeight = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.max(el.scrollHeight, minRows * 24) + 'px';
  }, [minRows]);

  useEffect(() => { adjustHeight(); }, [value, adjustHeight]);

  return (
    <textarea
      ref={ref}
      value={value}
      onChange={onChange}
      onInput={adjustHeight}
      disabled={disabled}
      placeholder={placeholder}
      rows={minRows}
      className="w-full bg-gray-900 border border-gray-700 rounded-lg px-4 py-3 text-gray-200 focus:border-purple-500 focus:outline-none resize-none overflow-hidden whitespace-pre-wrap break-words"
    />
  );
}

// The World Prompt textarea plus an "AI write" button that turns the player's
// notes (and the linked scenario, if any) into a full seed prompt — the same
// LLM-as-author pattern as the scenario editor's prompt rewrite. `onChange`
// takes the new string directly.
// The AI-write notes survive a relaunch too (Android kills the backgrounded
// PWA); cleared when the AI successfully writes the prompt from them.
const AI_NOTES_KEY = 'wb_worldgen_ai_notes';

function WorldPromptField({ value, onChange, disabled, scenarioId }) {
  const [open, setOpen] = useState(false);
  const [instruction, setInstruction] = useState(() => {
    try { return storage.getItem(AI_NOTES_KEY) || ''; } catch { return ''; }
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    try {
      if (instruction) storage.setItem(AI_NOTES_KEY, instruction);
      else storage.removeItem(AI_NOTES_KEY);
    } catch { /* storage unavailable */ }
  }, [instruction]);

  const runEnrich = async () => {
    if (busy) return;
    const instr = instruction.trim();
    const hasDraft = !!(value || '').trim();
    if (!instr && !hasDraft && !scenarioId) {
      setError('Jot down some direction, or link a scenario first.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const res = await api.rewriteWorldPrompt({
        instruction: instr,
        currentText: (value || '').trim() || null,
        scenarioId: scenarioId || null,
      });
      onChange(res.text);
      setInstruction('');
      setOpen(false);
    } catch (e) {
      setError(e.message || 'AI write failed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 mb-2">
        <label className="block text-sm font-medium text-gray-400">World Prompt</label>
        <button
          type="button"
          onClick={() => { setOpen((v) => !v); setError(''); }}
          disabled={disabled}
          title="Let the AI write a world prompt from your notes and the linked scenario"
          className={`shrink-0 px-2 py-1 rounded text-xs border transition-colors disabled:opacity-50 ${
            open
              ? 'border-purple-500 text-purple-300 bg-purple-900/30'
              : 'border-gray-700 text-gray-400 hover:bg-gray-700'
          }`}
        >
          ✨ AI write
        </button>
      </div>
      <AutoTextarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. A post-apocalyptic Earth where fungi have evolved sentience and built civilizations beneath the surface..."
        disabled={disabled}
      />
      {open && (
        <div className="mt-2 rounded-lg border border-purple-800/50 bg-purple-950/20 p-3 space-y-2">
          <p className="text-xs text-gray-400">
            Jot down your ideas and the AI turns them — together with the linked scenario, if any —
            into a full world prompt above. Leave blank to draft purely from the scenario.
          </p>
          <div className="flex items-center gap-2">
            <input
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); runEnrich(); } }}
              disabled={disabled || busy}
              placeholder='e.g. "a drowned city ruled by three rival guilds"'
              className="flex-1 px-3 py-1.5 rounded-lg bg-gray-900 border border-gray-700 text-xs text-gray-200 placeholder-gray-600 focus:outline-none focus:border-purple-500"
            />
            <button
              type="button"
              onClick={runEnrich}
              disabled={disabled || busy}
              className="shrink-0 px-3 py-1.5 rounded-lg text-xs bg-purple-700 hover:bg-purple-600 disabled:opacity-50 disabled:cursor-not-allowed text-white transition-colors"
            >
              {busy ? 'Writing…' : 'Write'}
            </button>
          </div>
          {error && <p className="text-xs text-red-400">{error}</p>}
        </div>
      )}
    </div>
  );
}

// The pre-build form (prompt, scenario) plus the pinned agent-build world
// id, mirrored to localStorage on every change: Android kills the
// backgrounded PWA, and a prompt that was typed (or AI-written) but not yet
// handed to a build exists nowhere else. The pin is what routes a relaunch
// straight back to the running build's observer — the loop runs server-side
// and survives the frontend.
export const FORM_KEY = 'wb_worldgen_wizard_form';

export function readSavedForm() {
  try {
    return JSON.parse(storage.getItem(FORM_KEY) || 'null') || {};
  } catch {
    return {};
  }
}

// Pin an agent build so the create screen opens on its observer — used by
// the world list's recovery affordances (reattach / finish-with-AI).
export function pinAgentBuild(worldId) {
  try {
    storage.setItem(FORM_KEY, JSON.stringify({ ...readSavedForm(), agentWorldId: worldId }));
  } catch { /* storage unavailable — the observer is still reachable via recovery */ }
}

/**
 * WorldCreateScreen — the front door to building a world (C7b): the first
 * message to the design partner opens a draft world and its agent session;
 * from there the one continuous screen (chat + drafts + Go + build) is the
 * AgentBuildObserver. The prompt field and "Build this world" remain the
 * zero-turn path — a direct launch with no conversation.
 */
export default function WorldCreateScreen({ onBack, onOpenWorlds, onExploreWorld }) {
  const [loading, setLoading] = useState(false);
  const [seedPrompt, setSeedPrompt] = useState(() => readSavedForm().seedPrompt || '');
  const [chatDraft, setChatDraft] = useState('');
  const [scenarios, setScenarios] = useState([]);
  const [scenarioId, setScenarioId] = useState(() => readSavedForm().scenarioId || null);
  // A running (or finished, not yet dismissed) agent session owned by this
  // client. Mirrored to localStorage so a relaunched client reattaches.
  const [agentWorldId, setAgentWorldId] = useState(() => readSavedForm().agentWorldId || null);

  useEffect(() => {
    api.listScenarios()
      .then((data) => {
        const list = data.scenarios || [];
        setScenarios(list);
        setScenarioId((cur) => (cur && !list.some((s) => s.id === cur) ? null : cur));
      })
      .catch(() => {});
  }, []);

  // Mirror the form so a relaunch before the session starts loses nothing.
  useEffect(() => {
    try {
      storage.setItem(FORM_KEY, JSON.stringify({ seedPrompt, scenarioId, agentWorldId }));
    } catch { /* storage unavailable */ }
  }, [seedPrompt, scenarioId, agentWorldId]);

  // The zero-turn go (C4, kept by C7b): hand the typed prompt straight to
  // the build agent — no conversation, a brief with empty rules and notes.
  const handleAgentStart = async () => {
    if (!seedPrompt.trim()) return;
    setLoading(true);
    try {
      const result = await api.agentBuild(seedPrompt.trim(), scenarioId);
      setAgentWorldId(result.world_id);
    } catch (e) {
      alert('Failed to start the agent build: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  // The conversational front door (C7b): the first message lazily creates
  // the draft world and opens the session's chat phase — everything after
  // this happens on the session screen, where the drafts are server truth.
  const handleChatStart = async () => {
    const text = chatDraft.trim();
    if (!text || loading) return;
    setLoading(true);
    try {
      const result = await api.agentChatStart({
        text,
        prompt: seedPrompt.trim(),
        scenarioId: scenarioId || null,
      });
      setChatDraft('');
      // The draft world owns the prompt from here (server truth) — a
      // leftover in the form would silently seed the NEXT draft too.
      setSeedPrompt('');
      setAgentWorldId(result.world_id);
    } catch (e) {
      alert('Failed to start the conversation: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  if (agentWorldId) {
    return (
      <AgentBuildObserver
        worldId={agentWorldId}
        onDismiss={() => setAgentWorldId(null)}
        onOpenWorlds={() => { setAgentWorldId(null); onOpenWorlds?.(); }}
        onExplore={onExploreWorld
          ? () => { const id = agentWorldId; setAgentWorldId(null); onExploreWorld(id); }
          : undefined}
        onBack={onBack}
      />
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center p-6">
      <div className="w-full max-w-lg mt-16">
        <button
          onClick={onBack}
          className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-8"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Menu
        </button>

        <div className="bg-gray-800/60 border border-gray-700 rounded-xl p-6 space-y-6">
          <div>
            <h2 className="text-2xl font-bold text-gray-100 mb-2">World Generation</h2>
            <p className="text-gray-400 text-sm">
              Describe the world you want to create — any genre, any scale. Shape the idea
              in conversation with the AI, then hand it off: an agent plans, builds and
              verifies the whole world to fit.
            </p>
          </div>

          {scenarios.length > 0 && (
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-2">Link a Scenario <span className="text-gray-600">(optional)</span></label>
              <p className="text-xs text-gray-500 mb-2">
                The world is built to contain the linked scenario's setting and opening scene,
                and creating a story from this world starts that scenario in it.
              </p>
              <div className="grid gap-2">
                <button
                  type="button"
                  onClick={() => setScenarioId(null)}
                  disabled={loading}
                  className={`text-left px-3 py-2 rounded-lg border transition-colors ${
                    !scenarioId
                      ? 'border-purple-500 bg-purple-900/30'
                      : 'border-gray-700 bg-gray-800/40 hover:border-gray-500'
                  }`}
                >
                  <div className="text-sm font-medium text-gray-200">No Scenario</div>
                  <div className="text-xs text-gray-500 mt-0.5">Build the world from the prompt alone</div>
                </button>
                {scenarios.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => setScenarioId(s.id)}
                    disabled={loading}
                    className={`text-left px-3 py-2 rounded-lg border transition-colors ${
                      scenarioId === s.id
                        ? 'border-purple-500 bg-purple-900/30'
                        : 'border-gray-700 bg-gray-800/40 hover:border-gray-500'
                    }`}
                  >
                    <div className="text-sm font-medium text-gray-200">{s.name}</div>
                    <div className="text-xs text-gray-500 mt-0.5">
                      Grounds the world in this scenario's description{s.has_starting_prompt ? ' and opening scene' : ''}
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )}

          <WorldPromptField
            value={seedPrompt}
            onChange={setSeedPrompt}
            disabled={loading}
            scenarioId={scenarioId}
          />

          <div className="rounded-lg border border-emerald-900/60 bg-emerald-950/10 p-3 space-y-3">
            <div>
              <p className="text-sm font-medium text-gray-200">Design it with the AI</p>
              <p className="text-xs text-gray-500">
                Start a conversation — the AI shapes the prompt, world rules and design
                notes with you, and you press build when it feels right. Your first
                message opens a draft world you can leave and come back to any time.
              </p>
            </div>
            <div className="flex gap-2 items-end">
              <textarea
                value={chatDraft}
                onChange={(e) => setChatDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    handleChatStart();
                  }
                }}
                rows={2}
                disabled={loading}
                placeholder="What kind of world are you dreaming of?"
                className="flex-1 bg-gray-900/80 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-emerald-600 resize-none"
              />
              <button
                type="button"
                onClick={handleChatStart}
                disabled={!chatDraft.trim() || loading}
                className="shrink-0 px-3 py-2 rounded-lg text-sm bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 disabled:cursor-not-allowed text-white transition-colors"
              >
                {loading ? 'Starting…' : 'Start designing'}
              </button>
            </div>
          </div>

          <div>
            <button
              type="button"
              onClick={handleAgentStart}
              disabled={!seedPrompt.trim() || loading}
              className="w-full py-3 rounded-lg font-medium text-lg border border-emerald-800 text-emerald-300 hover:bg-emerald-950/40 disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center justify-center gap-2"
            >
              {loading && (
                <span className="inline-block w-5 h-5 border-2 border-emerald-300/30 border-t-emerald-300 rounded-full animate-spin" />
              )}
              {loading ? 'Starting…' : 'Build this world'}
            </button>
            <p className="text-xs text-gray-500 mt-2">
              The direct route: hand the prompt above straight to the agent with no
              conversation. It plans, builds and verifies the whole world server-side —
              you can watch, keep talking to it, or come back later.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
