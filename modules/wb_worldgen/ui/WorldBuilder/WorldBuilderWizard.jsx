import { useState, useEffect, useRef, useCallback } from 'react';
import { api } from 'api';
import StepCard from './StepCard';

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
    try { return localStorage.getItem(AI_NOTES_KEY) || ''; } catch { return ''; }
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    try {
      if (instruction) localStorage.setItem(AI_NOTES_KEY, instruction);
      else localStorage.removeItem(AI_NOTES_KEY);
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

// Iterative interview that digs unmentioned details out of the player before
// generation: the AI reads the prompt (empty is fine — then it interviews from
// scratch) and asks a round of clarifying questions; answered ones are folded
// back into the prompt — every answer lands, adding and rewriting what it
// touches while the rest keeps the player's wording. Repeatable until the
// player is happy — accepting is simply
// generating. Prior rounds are sent along so questions never repeat, and the
// in-flight round survives a relaunch (Android kills the backgrounded PWA).
const INTERVIEW_KEY = 'wb_worldgen_interview';

function readSavedInterview() {
  try {
    return JSON.parse(localStorage.getItem(INTERVIEW_KEY) || 'null') || {};
  } catch {
    return {};
  }
}

function WorldPromptInterview({ promptText, onPromptChange, scenarioId, disabled }) {
  const [questions, setQuestions] = useState(() => readSavedInterview().questions || null);
  const [answers, setAnswers] = useState(() => readSavedInterview().answers || []);
  const [history, setHistory] = useState(() => readSavedInterview().history || []);
  const [busy, setBusy] = useState(null); // 'ask' | 'fold' | null
  const [error, setError] = useState('');

  useEffect(() => {
    try {
      if (questions || history.length) {
        localStorage.setItem(INTERVIEW_KEY, JSON.stringify({ questions, answers, history }));
      } else {
        localStorage.removeItem(INTERVIEW_KEY);
      }
    } catch { /* storage unavailable */ }
  }, [questions, answers, history]);

  const ask = async () => {
    if (busy) return;
    setBusy('ask');
    setError('');
    try {
      const res = await api.worldPromptQuestions({
        currentText: (promptText || '').trim() || null,
        history,
        scenarioId: scenarioId || null,
      });
      setQuestions(res.questions);
      setAnswers(res.questions.map(() => ''));
    } catch (e) {
      setError(e.message || 'Failed to get questions.');
    } finally {
      setBusy(null);
    }
  };

  const closeRound = (roundPairs) => {
    setHistory((h) => [...h, ...roundPairs]);
    setQuestions(null);
    setAnswers([]);
  };

  const fold = async () => {
    if (busy) return;
    const pairs = questions.map((q, i) => ({ question: q, answer: (answers[i] || '').trim() }));
    if (!pairs.some((p) => p.answer)) return;
    setBusy('fold');
    setError('');
    try {
      const res = await api.foldWorldAnswers({
        currentText: (promptText || '').trim() || null,
        answers: pairs.filter((p) => p.answer),
        scenarioId: scenarioId || null,
      });
      onPromptChange(res.text);
      closeRound(pairs);
    } catch (e) {
      setError(e.message || 'Failed to update the prompt.');
    } finally {
      setBusy(null);
    }
  };

  const dismiss = () => {
    if (busy) return;
    // The whole round counts as skipped — typed-but-not-folded answers never
    // reached the prompt, so they must not read as settled next round.
    closeRound(questions.map((q) => ({ question: q, answer: '' })));
    setError('');
  };

  const hasAnswer = answers.some((a) => (a || '').trim());

  if (!questions) {
    return (
      <div className="space-y-1">
        <button
          type="button"
          onClick={ask}
          disabled={disabled || !!busy}
          title="The AI asks about details your prompt leaves open, then works your answers into it"
          className="px-3 py-1.5 rounded-lg text-xs border border-gray-700 text-gray-400 hover:bg-gray-700 disabled:opacity-50 transition-colors"
        >
          {busy === 'ask'
            ? 'Thinking of questions…'
            : history.length ? '❓ Ask more questions' : '❓ Refine with questions'}
        </button>
        {error && <p className="text-xs text-red-400">{error}</p>}
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-purple-800/50 bg-purple-950/20 p-3 space-y-3">
      <p className="text-xs text-gray-400">
        Answer the questions you care about and skip the rest — skipped ones are left
        for the generator to decide.
      </p>
      {questions.map((q, i) => (
        <div key={i}>
          <p className="text-sm text-gray-300 mb-1">{q}</p>
          <AutoTextarea
            value={answers[i] || ''}
            onChange={(e) => {
              const next = answers.slice();
              next[i] = e.target.value;
              setAnswers(next);
            }}
            disabled={disabled || !!busy}
            minRows={1}
            placeholder="(leave blank to skip)"
          />
        </div>
      ))}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={fold}
          disabled={disabled || !!busy || !hasAnswer}
          className="px-3 py-1.5 rounded-lg text-xs bg-purple-700 hover:bg-purple-600 disabled:opacity-50 disabled:cursor-not-allowed text-white transition-colors"
        >
          {busy === 'fold' ? 'Updating prompt…' : 'Add answers to prompt'}
        </button>
        <button
          type="button"
          onClick={dismiss}
          disabled={disabled || !!busy}
          className="px-3 py-1.5 rounded-lg text-xs border border-gray-700 text-gray-400 hover:bg-gray-700 disabled:opacity-50 transition-colors"
        >
          Skip these
        </button>
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  );
}

// The pre-generation form (prompt, scenario, skip-review) mirrored
// to localStorage on every change: Android kills the backgrounded PWA, and a
// prompt that was typed (or AI-written) but not yet generated exists nowhere
// else. Cleared when the world is saved.
const FORM_KEY = 'wb_worldgen_wizard_form';

function readSavedForm() {
  try {
    return JSON.parse(localStorage.getItem(FORM_KEY) || 'null') || {};
  } catch {
    return {};
  }
}

export default function WorldBuilderWizard({ onBack, onWorldCreated }) {
  const [pipeline, setPipeline] = useState([]);
  const [worldState, setWorldState] = useState(null);
  const [currentStepId, setCurrentStepId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [skipReview, setSkipReview] = useState(() => !!readSavedForm().skipReview);
  const [seedPrompt, setSeedPrompt] = useState(() => readSavedForm().seedPrompt || '');
  const [scenarios, setScenarios] = useState([]);
  const [scenarioId, setScenarioId] = useState(() => readSavedForm().scenarioId || null);
  const [started, setStarted] = useState(false);
  // A running (or finished, not yet dismissed) agent build owned by this
  // client. The id is mirrored to localStorage so a relaunched client
  // reattaches — the loop runs server-side and survives the frontend.
  const [agentWorldId, setAgentWorldId] = useState(() => readSavedForm().agentWorldId || null);
  const [agentStatus, setAgentStatus] = useState(null);
  // The effective (post-skip) step order for the running session: the world's
  // own World Design step can turn steps off, which the statically-fetched
  // pipeline can't know. null until the server has told us.
  const [effectiveSteps, setEffectiveSteps] = useState(null);

  useEffect(() => {
    api.listScenarios()
      .then((data) => {
        const list = data.scenarios || [];
        setScenarios(list);
        setScenarioId((cur) => (cur && !list.some((s) => s.id === cur) ? null : cur));
      })
      .catch(() => {});
  }, []);

  // Mirror the form so a relaunch before generation starts loses nothing.
  useEffect(() => {
    try {
      localStorage.setItem(FORM_KEY, JSON.stringify({ seedPrompt, scenarioId, skipReview, agentWorldId }));
    } catch { /* storage unavailable — the server draft still covers started runs */ }
  }, [seedPrompt, scenarioId, skipReview, agentWorldId]);

  // Follow an agent build: poll its status while it runs; reattach after a
  // relaunch (404 = the world/build is gone — drop the stale reference).
  useEffect(() => {
    if (!agentWorldId) { setAgentStatus(null); return undefined; }
    let alive = true;
    const poll = async () => {
      try {
        const st = await api.agentBuildStatus(agentWorldId);
        if (alive) setAgentStatus(st);
      } catch (e) {
        if (alive && e.status === 404) setAgentWorldId(null);
      }
    };
    poll();
    const t = setInterval(() => {
      // Keep polling until a terminal status has been painted.
      if (!agentStatus || agentStatus.status === 'running') poll();
    }, 2000);
    return () => { alive = false; clearInterval(t); };
  }, [agentWorldId, agentStatus?.status]);

  useEffect(() => {
    api.getWorldPipeline()
      .then((data) => setPipeline(data.pipeline || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    // Check for an existing draft (or a generation still running server-side)
    // to resume. A relaunch while the very first step was generating has no
    // steps yet — state._generating is what says there's something to return
    // to; the poll effect below then follows the run to completion.
    api.getWorldState().then((data) => {
      const st = data.state;
      const hasSession = !!st && (st._generating || !!st.seed_prompt
        || Object.keys(st.steps || {}).length > 0);
      if (hasSession) {
        setWorldState(st);
        if (data.effective_steps) setEffectiveSteps(data.effective_steps);
        setStarted(true);
        setSkipReview(!!st.skip_review);
        if (st.steps?.lore?.data?.world_name) {
          setSeedPrompt(st.seed_prompt || '');
        }
        setScenarioId(st.scenario_id || null);
        if (!st.complete) {
          setCurrentStepId(st.current_step);
        }
        // A run that stopped without finishing and left nothing awaiting
        // review: the generating request (or the whole backend process) was
        // killed while the app was minimized. Kick it back off — one-shot
        // sessions rerun the remaining pipeline, review-mode sessions
        // regenerate the step that was in flight; the poll paints progress.
        const reviewable = Object.values(st.steps || {}).some((s) => s?.data && !s?.approved);
        if (!st.complete && !st._generating && (st.skip_review || !reviewable)) {
          setWorldState({ ...st, _generating: st.skip_review ? 'all' : (st.current_step || 'next') });
          api.continueWorldGeneration().then((cont) => {
            setWorldState(cont.state);
            if (cont.effective_steps) setEffectiveSteps(cont.effective_steps);
            if (cont.state?.complete) setCurrentStepId(null);
            else if (cont.state?.current_step) setCurrentStepId(cont.state.current_step);
          }).catch(() => {});
        }
      }
    }).catch(() => {});
  }, []);

  // While the server reports a generation in flight (state._generating), poll
  // the session state. This is what lets a relaunched client — Android kills
  // the backgrounded PWA, losing the original request's response — pick the
  // run back up: the server keeps generating regardless, and each poll paints
  // the steps finished so far. Harmless alongside the original request's own
  // await (same data lands twice). One-shot sessions poll until complete even
  // without the flag: the mount effect's continue call may not have reached
  // the server yet, and a poll response from that gap must not stop the loop.
  const serverBusy = !!worldState?._generating;
  const polling = serverBusy || (!!worldState?.skip_review && !worldState?.complete);
  useEffect(() => {
    if (!started || !polling) return undefined;
    let alive = true;
    const t = setInterval(async () => {
      try {
        const data = await api.getWorldState();
        if (!alive || !data.state?.steps) return;
        setWorldState(data.state);
        if (data.effective_steps) setEffectiveSteps(data.effective_steps);
        // Only move the step pointer once the run has finished — mid-flight
        // current_step still names the previous step.
        if (!data.state._generating) {
          if (data.state.complete) setCurrentStepId(null);
          else if (data.state.current_step) setCurrentStepId(data.state.current_step);
        }
      } catch { /* transient — keep polling */ }
    }, 2500);
    return () => { alive = false; clearInterval(t); };
  }, [started, polling]);

  const handleStart = async () => {
    if (!seedPrompt.trim()) return;
    setLoading(true);
    try {
      const result = await api.generateWorld(seedPrompt.trim(), skipReview, scenarioId);
      setWorldState(result.state);
      if (result.effective_steps) setEffectiveSteps(result.effective_steps);
      setStarted(true);

      if (skipReview) {
        api.getWorldState().then((data) => {
          setWorldState(data.state);
          if (data.effective_steps) setEffectiveSteps(data.effective_steps);
        });
      } else {
        setCurrentStepId(result.current_step);
      }
    } catch (e) {
      alert('Failed to start world generation: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleAgentStart = async () => {
    if (!seedPrompt.trim()) return;
    setLoading(true);
    try {
      const result = await api.agentBuild(seedPrompt.trim(), scenarioId);
      setAgentStatus(null);
      setAgentWorldId(result.world_id);
    } catch (e) {
      alert('Failed to start the agent build: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleReroll = async (stepId, editedData = null) => {
    const stepState = worldState?.steps?.[stepId];
    const note = stepState?.note || '';
    setLoading(true);
    try {
      const result = await api.generateWorldStep(stepId, note, editedData);
      setWorldState(result.state);
      if (result.effective_steps) setEffectiveSteps(result.effective_steps);
      if (result.state.current_step) {
        setCurrentStepId(result.state.current_step);
      }
    } catch (e) {
      alert('Re-roll failed: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (stepId, editedData) => {
    setLoading(true);
    try {
      const result = await api.approveWorldStep(stepId, editedData);
      setWorldState(result.state);
      if (result.effective_steps) setEffectiveSteps(result.effective_steps);

      if (result.complete) {
        setCurrentStepId(null);
      } else {
        setCurrentStepId(result.current_step);
      }
    } catch (e) {
      alert('Failed to approve step: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleRerollItem = async (stepId, field, index, items, opts = {}) => {
    const stepState = worldState?.steps?.[stepId];
    const note = opts.note || stepState?.note || '';
    try {
      const result = await api.regenerateWorldItem(stepId, field, index, items, note, opts.subfield || null);
      return result.item;
    } catch (e) {
      alert('Re-roll item failed: ' + e.message);
      return null;
    }
  };

  const handleAddNote = async (stepId, note) => {
    setLoading(true);
    try {
      const result = await api.generateWorldStep(stepId, note);
      setWorldState(result.state);
      if (result.effective_steps) setEffectiveSteps(result.effective_steps);
      if (result.state.current_step) {
        setCurrentStepId(result.state.current_step);
      }
    } catch (e) {
      alert('Failed to regenerate with note: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleEnrichCommit = async (stepId) => {
    const worldId = worldState?._draft_id;
    if (!worldId) return;
    setLoading(true);
    try {
      const result = await api.enrichCommit(worldId, stepId);
      if (result.state) {
        setWorldState(result.state);
      }
    } catch (e) {
      alert('Enrich commit failed: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleReApprove = async (stepId, editedData) => {
    if (!editedData) return;
    setLoading(true);
    try {
      const result = await api.approveWorldStep(stepId, editedData);
      setWorldState(result.state);
      if (result.effective_steps) setEffectiveSteps(result.effective_steps);

      if (result.current_step) {
        setCurrentStepId(result.current_step);
      } else if (result.complete) {
        setCurrentStepId(null);
      }
    } catch (e) {
      alert('Failed to save changes: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleCompile = async () => {
    setLoading(true);
    try {
      const worldId = worldState?.steps?.lore?.data?.world_name || 'world_gen';
      await api.saveWorld(worldId);
      try {
        localStorage.removeItem(FORM_KEY);
        localStorage.removeItem(INTERVIEW_KEY);
      } catch { /* ignore */ }
      setWorldState(null);
      setStarted(false);
      setCurrentStepId(null);
      onWorldCreated?.();
    } catch (e) {
      alert('Failed to save world: ' + e.message);
    } finally {
      setLoading(false);
    }
  };

  if (agentWorldId) {
    // Minimal agent-build watch card (the full observer UI is C3): live
    // status + todo from the server-side loop, cancel, reattach-safe.
    const st = agentStatus;
    const running = !st || st.status === 'running';
    const statusLabel = !st ? 'connecting…' : {
      running: 'building…', done: 'finished', cancelled: 'cancelled',
      failed: 'failed', budget_exhausted: 'stopped (budget exhausted)',
    }[st.status] || st.status;
    const todoIcon = { done: '✓', in_progress: '➤', pending: '○' };
    const dismiss = () => { setAgentWorldId(null); setAgentStatus(null); };
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center p-6">
        <div className="w-full max-w-lg mt-16">
          <div className="bg-gray-800/60 border border-gray-700 rounded-xl p-6 space-y-4">
            <div>
              <h2 className="text-2xl font-bold text-gray-100 mb-1 flex items-center gap-3">
                {running && (
                  <span className="inline-block w-5 h-5 border-2 border-emerald-400/30 border-t-emerald-400 rounded-full animate-spin" />
                )}
                Agent build — {statusLabel}
              </h2>
              <p className="text-gray-500 text-xs">
                World <span className="text-gray-400">{agentWorldId}</span>
                {st ? ` · turn ${st.turns} · ${st.tool_calls} tool calls` : ''}
              </p>
            </div>
            {st?.seed_prompt && (
              <p className="text-sm text-gray-400 border-l-2 border-gray-700 pl-3">{st.seed_prompt}</p>
            )}
            {(st?.todo?.length ?? 0) > 0 && (
              <ul className="space-y-1">
                {st.todo.map((item, i) => (
                  <li
                    key={i}
                    className={`text-sm flex gap-2 ${
                      item.status === 'done' ? 'text-gray-500 line-through'
                        : item.status === 'in_progress' ? 'text-emerald-300'
                          : 'text-gray-300'
                    }`}
                  >
                    <span className="w-4 text-center">{todoIcon[item.status] || '○'}</span>
                    {item.text}
                  </li>
                ))}
              </ul>
            )}
            {running && st?.last_event && (
              <p className="text-xs text-gray-500">
                {st.last_event.type === 'action'
                  ? `Running ${st.last_event.tool}…`
                  : st.last_event.type === 'eval'
                    ? `Evaluating — ${st.last_event.blocking ?? 0} blocking finding(s)`
                    : st.last_event.type === 'turn' && st.last_event.thought
                      ? st.last_event.thought
                      : null}
              </p>
            )}
            {st?.status === 'done' && st?.result?.summary && (
              <p className="text-sm text-emerald-300">{st.result.summary}</p>
            )}
            {st?.error && <p className="text-sm text-red-400">{st.error}</p>}
            <div className="flex gap-3 pt-1">
              {running ? (
                <button
                  onClick={() => api.agentBuildCancel(agentWorldId).catch(() => {})}
                  className="px-4 py-2 rounded-lg border border-red-900 text-red-300 hover:bg-red-950/40 text-sm transition-colors"
                >
                  Cancel build
                </button>
              ) : (
                <>
                  {st?.status === 'done' && (
                    <button
                      onClick={() => { dismiss(); onWorldCreated?.(); }}
                      className="px-4 py-2 rounded-lg bg-emerald-700 hover:bg-emerald-600 text-sm font-medium transition-colors"
                    >
                      Open world list
                    </button>
                  )}
                  <button
                    onClick={dismiss}
                    className="px-4 py-2 rounded-lg border border-gray-700 text-gray-400 hover:bg-gray-700 text-sm transition-colors"
                  >
                    {st?.status === 'done' ? 'Dismiss' : 'Back to the prompt'}
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!started) {
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
                Describe the world you want to create — any genre, any scale. The AI reads your
                prompt and shapes the generation to fit, from a fantasy overworld to a single
                modern city.
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

            <WorldPromptInterview
              promptText={seedPrompt}
              onPromptChange={setSeedPrompt}
              scenarioId={scenarioId}
              disabled={loading}
            />

            <div className="flex items-center gap-3">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={skipReview}
                  onChange={(e) => setSkipReview(e.target.checked)}
                  className="accent-purple-500"
                  disabled={loading}
                />
                <span className="text-sm text-gray-400">Skip review — generate all at once</span>
              </label>
            </div>

            <button
              onClick={handleStart}
              disabled={loading || !seedPrompt.trim()}
              className="w-full py-3 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded-lg font-medium text-lg transition-colors flex items-center justify-center gap-2"
            >
              {loading && (
                <span className="inline-block w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              )}
              {loading ? 'Generating...' : 'Generate World'}
            </button>

            <div>
              <button
                onClick={handleAgentStart}
                disabled={loading || !seedPrompt.trim()}
                className="w-full py-3 bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 rounded-lg font-medium text-lg transition-colors"
              >
                Let the AI build it
              </button>
              <p className="text-xs text-gray-500 mt-2">
                An agent plans, builds and verifies the whole world on its own —
                watch it work, or come back later. It runs server-side, so
                closing the app doesn't stop it.
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const currentStep = pipeline.find((s) => s.id === currentStepId);
  // Steps the world's own design turned off (template-skipped steps never
  // reach `pipeline` in the first place). Until the server has reported an
  // effective order, everything counts as active.
  const effectiveIds = effectiveSteps ?? pipeline.map((s) => s.id);
  const skippedIds = new Set(pipeline.filter((s) => !effectiveIds.includes(s.id)).map((s) => s.id));
  const visiblePipeline = pipeline.filter((s) => !skippedIds.has(s.id));
  const approvedSteps = visiblePipeline.filter((s) => worldState?.steps?.[s.id]?.approved);
  const complete = worldState?.complete;

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
            Exit
          </button>
          <div className="text-sm text-gray-400">
            {!complete && (
              <span>Step {approvedSteps.length + 1} of {visiblePipeline.length}</span>
            )}
          </div>
        </div>

        {pipeline
          .filter((step) => skippedIds.has(step.id) || worldState?.steps?.[step.id]?.approved)
          .map((step) => (
            skippedIds.has(step.id) ? (
              <div
                key={step.id}
                title="Edit the World Design step to bring this step back"
                className="bg-gray-800/30 border border-gray-800 rounded-xl px-6 py-3 flex items-center justify-between"
              >
                <span className="text-sm text-gray-500">{step.label}</span>
                <span className="text-xs text-gray-600">Skipped by world design</span>
              </div>
            ) : (
              <StepCard
                key={step.id}
                step={step}
                state={worldState?.steps?.[step.id]}
                onApprove={(data) => handleReApprove(step.id, data)}
                onReroll={(data) => handleReroll(step.id, data)}
                onAddNote={(note) => handleAddNote(step.id, note)}
                onRerollItem={handleRerollItem}
                onEnrichCommit={(stepId) => handleEnrichCommit(stepId)}
                loading={loading || serverBusy}
                worldId={worldState?._draft_id}
                worldState={worldState}
              />
            )
          ))}

        {complete ? (
          <div className="bg-gray-800/80 border border-purple-700 rounded-xl p-6 text-center space-y-4">
            <div className="text-4xl">🌍</div>
            <h3 className="text-2xl font-bold text-purple-300">World Complete</h3>
            <p className="text-gray-400">
              Your world has been generated across {visiblePipeline.length} stages. Review the details above, then save your world.
            </p>
            <button
              onClick={handleCompile}
              disabled={loading}
              className="px-8 py-3 bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded-lg font-medium text-lg transition-colors flex items-center gap-2 mx-auto"
            >
              {loading && (
                <span className="inline-block w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              )}
              {loading ? 'Saving...' : 'Save World'}
            </button>
          </div>
        ) : skipReview ? (
          <div className="bg-gray-800/80 border border-purple-700 rounded-xl p-6 text-center space-y-4">
            <div className="inline-block w-8 h-8 border-2 border-purple-400/30 border-t-purple-400 rounded-full animate-spin" />
            <p className="text-gray-400">Generating all world stages...</p>
          </div>
        ) : currentStep && !worldState?.steps?.[currentStepId]?.approved ? (
          <StepCard
            step={currentStep}
            state={worldState?.steps?.[currentStepId]}
            onApprove={(data) => handleApprove(currentStepId, data)}
            onReroll={(data) => handleReroll(currentStepId, data)}
            onAddNote={(note) => handleAddNote(currentStepId, note)}
            onRerollItem={handleRerollItem}
            onEnrichCommit={(stepId) => handleEnrichCommit(stepId)}
            loading={loading || serverBusy}
            worldId={worldState?._draft_id}
            worldState={worldState}
          />
        ) : serverBusy ? (
          // Relaunched while a step was still generating (the reviewable card
          // for it doesn't exist yet) — the poll effect swaps this for the
          // finished step when the server is done.
          <div className="bg-gray-800/80 border border-purple-700 rounded-xl p-6 text-center space-y-4">
            <div className="inline-block w-8 h-8 border-2 border-purple-400/30 border-t-purple-400 rounded-full animate-spin" />
            <p className="text-gray-400">Generating the next stage...</p>
          </div>
        ) : null}
      </div>
    </div>
  );
}
