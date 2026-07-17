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
function WorldPromptField({ value, onChange, disabled, scenarioId }) {
  const [open, setOpen] = useState(false);
  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

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

export default function WorldBuilderWizard({ onBack, onWorldCreated }) {
  const [pipeline, setPipeline] = useState([]);
  const [worldState, setWorldState] = useState(null);
  const [currentStepId, setCurrentStepId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [skipReview, setSkipReview] = useState(false);
  const [seedPrompt, setSeedPrompt] = useState('');
  const [scenarios, setScenarios] = useState([]);
  const [scenarioId, setScenarioId] = useState(null);
  const [started, setStarted] = useState(false);
  const [templates, setTemplates] = useState([]);
  const [templateId, setTemplateId] = useState('ai_default');
  // The effective (post-skip) step order for the running session: the world's
  // own World Design step can turn steps off, which the statically-fetched
  // pipeline can't know. null until the server has told us.
  const [effectiveSteps, setEffectiveSteps] = useState(null);

  useEffect(() => {
    api.getWorldTemplates()
      .then((data) => setTemplates(data.templates || []))
      .catch(() => {});
    api.listScenarios()
      .then((data) => setScenarios(data.scenarios || []))
      .catch(() => {});
  }, []);

  // The pipeline (steps + schemas) depends on the chosen template.
  useEffect(() => {
    api.getWorldPipeline(templateId)
      .then((data) => setPipeline(data.pipeline || []))
      .catch(() => {});
  }, [templateId]);

  useEffect(() => {
    // Check for an existing draft (or a generation still running server-side)
    // to resume. A relaunch while the very first step was generating has no
    // steps yet — state._generating is what says there's something to return
    // to; the poll effect below then follows the run to completion.
    api.getWorldState().then((data) => {
      const st = data.state;
      if (st?.steps && (Object.keys(st.steps).length > 0 || st._generating)) {
        setWorldState(st);
        if (data.effective_steps) setEffectiveSteps(data.effective_steps);
        setStarted(true);
        setSkipReview(!!st.skip_review);
        if (st.template_id) {
          setTemplateId(st.template_id);
        }
        if (st.steps?.lore?.data?.world_name) {
          setSeedPrompt(st.seed_prompt || '');
        }
        setScenarioId(st.scenario_id || null);
        if (!st.complete) {
          setCurrentStepId(st.current_step);
        }
      }
    }).catch(() => {});
  }, []);

  // While the server reports a generation in flight (state._generating), poll
  // the session state. This is what lets a relaunched client — Android kills
  // the backgrounded PWA, losing the original request's response — pick the
  // run back up: the server keeps generating regardless, and each poll paints
  // the steps finished so far. Harmless alongside the original request's own
  // await (same data lands twice).
  const serverBusy = !!worldState?._generating;
  useEffect(() => {
    if (!started || !serverBusy) return undefined;
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
  }, [started, serverBusy]);

  const handleStart = async () => {
    if (!seedPrompt.trim()) return;
    setLoading(true);
    try {
      const result = await api.generateWorld(seedPrompt.trim(), skipReview, templateId, scenarioId);
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

            {templates.length > 1 && (
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-2">World Type</label>
                <div className="grid gap-2">
                  {templates.map((t) => (
                    <button
                      key={t.id}
                      type="button"
                      onClick={() => setTemplateId(t.id)}
                      disabled={loading}
                      className={`text-left px-3 py-2 rounded-lg border transition-colors ${
                        templateId === t.id
                          ? 'border-purple-500 bg-purple-900/30'
                          : 'border-gray-700 bg-gray-800/40 hover:border-gray-500'
                      }`}
                    >
                      <div className="text-sm font-medium text-gray-200">{t.label || t.id}</div>
                      {t.description && (
                        <div className="text-xs text-gray-500 mt-0.5">{t.description}</div>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            )}

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
