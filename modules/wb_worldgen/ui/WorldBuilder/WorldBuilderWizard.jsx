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

export default function WorldBuilderWizard({ onBack, onWorldCreated }) {
  const [pipeline, setPipeline] = useState([]);
  const [worldState, setWorldState] = useState(null);
  const [currentStepId, setCurrentStepId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [skipReview, setSkipReview] = useState(false);
  const [seedPrompt, setSeedPrompt] = useState('');
  const [scenario, setScenario] = useState('');
  const [started, setStarted] = useState(false);
  const [templates, setTemplates] = useState([]);
  const [templateId, setTemplateId] = useState('overworld_fantasy');

  useEffect(() => {
    api.getWorldTemplates()
      .then((data) => setTemplates(data.templates || []))
      .catch(() => {});
  }, []);

  // The pipeline (steps + schemas) depends on the chosen template.
  useEffect(() => {
    api.getWorldPipeline(templateId)
      .then((data) => setPipeline(data.pipeline || []))
      .catch(() => {});
  }, [templateId]);

  useEffect(() => {
    // Check for existing draft to resume
    api.getWorldState().then((data) => {
      if (data.state?.steps && Object.keys(data.state.steps).length > 0) {
        setWorldState(data.state);
        setStarted(true);
        if (data.state.template_id) {
          setTemplateId(data.state.template_id);
        }
        if (data.state.steps?.lore?.data?.world_name) {
          setSeedPrompt(data.state.seed_prompt || '');
        }
        setScenario(data.state.scenario || '');
        if (!data.state.complete) {
          setCurrentStepId(data.state.current_step);
        }
      }
    }).catch(() => {});
  }, []);

  const handleStart = async () => {
    if (!seedPrompt.trim()) return;
    setLoading(true);
    try {
      const result = await api.generateWorld(seedPrompt.trim(), skipReview, templateId, scenario.trim());
      setWorldState(result.state);
      setStarted(true);

      if (skipReview) {
        api.getWorldState().then((data) => {
          setWorldState(data.state);
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
                Describe the world you want to create. The AI will generate rules, lore, and regions based on your prompt.
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

            <div>
              <label className="block text-sm font-medium text-gray-400 mb-2">Scenario <span className="text-gray-600">(optional)</span></label>
              <p className="text-xs text-gray-500 mb-2">
                Longer source material the world should be grounded in — a campaign setting, an adventure premise,
                pasted background text. The AI builds the world from this together with the prompt below.
              </p>
              <AutoTextarea
                value={scenario}
                onChange={(e) => setScenario(e.target.value)}
                minRows={2}
                placeholder="Paste or write the setting, situation, and any established facts, names or history the world must honor..."
                disabled={loading}
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-400 mb-2">World Prompt</label>
              <AutoTextarea
                value={seedPrompt}
                onChange={(e) => setSeedPrompt(e.target.value)}
                placeholder="e.g. A post-apocalyptic Earth where fungi have evolved sentience and built civilizations beneath the surface..."
                disabled={loading}
              />
            </div>

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
  const approvedSteps = pipeline.filter((s) => worldState?.steps?.[s.id]?.approved);
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
              <span>Step {approvedSteps.length + 1} of {pipeline.length}</span>
            )}
          </div>
        </div>

        {approvedSteps.map((step) => (
          <StepCard
            key={step.id}
            step={step}
            state={worldState?.steps?.[step.id]}
            onApprove={(data) => handleReApprove(step.id, data)}
            onReroll={(data) => handleReroll(step.id, data)}
            onAddNote={(note) => handleAddNote(step.id, note)}
            onRerollItem={handleRerollItem}
            onEnrichCommit={(stepId) => handleEnrichCommit(stepId)}
            loading={loading}
            worldId={worldState?._draft_id}
            worldState={worldState}
          />
        ))}

        {complete ? (
          <div className="bg-gray-800/80 border border-purple-700 rounded-xl p-6 text-center space-y-4">
            <div className="text-4xl">🌍</div>
            <h3 className="text-2xl font-bold text-purple-300">World Complete</h3>
            <p className="text-gray-400">
              Your world has been generated across {pipeline.length} stages. Review the details above, then save your world.
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
        ) : currentStep ? (
          <StepCard
            step={currentStep}
            state={worldState?.steps?.[currentStepId]}
            onApprove={(data) => handleApprove(currentStepId, data)}
            onReroll={(data) => handleReroll(currentStepId, data)}
            onAddNote={(note) => handleAddNote(currentStepId, note)}
            onRerollItem={handleRerollItem}
            onEnrichCommit={(stepId) => handleEnrichCommit(stepId)}
            loading={loading}
            worldId={worldState?._draft_id}
            worldState={worldState}
          />
        ) : null}
      </div>
    </div>
  );
}
