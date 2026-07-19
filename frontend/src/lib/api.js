const API = '';

async function request(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({}));
    throw new ApiError(res.status, error.detail || res.statusText);
  }
  return res.json();
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

export const api = {
  getSession:             () => request('/api/session'),
  getSaves:               () => request('/api/saves'),
  createSave:             (saveId, opts = {}) => request('/api/saves', { method: 'POST', body: JSON.stringify({
                            save_id: saveId,
                            world_id: opts.worldId ?? null,
                            scenario_id: opts.scenarioId ?? null,
                            start_preference: opts.startPreference ?? null,
                            start_location_node_id: opts.startLocationNodeId ?? null,
                            scenario_request: opts.scenarioRequest ?? null,
                            character_id: opts.characterId ?? null,
                            active_modules: opts.activeModules ?? null,
                            module_instructions: opts.moduleInstructions ?? null,
                            plot_likes: opts.plotLikes ?? null,
                            plot_dislikes: opts.plotDislikes ?? null,
                          }) }),
  loadSave:               (saveId) => request(`/api/saves/${saveId}/load`, { method: 'POST' }),
  undoSave:               (saveId, targetTurn) => request(`/api/saves/${saveId}/undo`, { method: 'POST', body: JSON.stringify({ target_turn: targetTurn }) }),
  selectSwipe:            (index) => request('/api/session/swipe', { method: 'POST', body: JSON.stringify({ index }) }),
  editMessage:            (index, content) => request(`/api/session/messages/${index}`, { method: 'PUT', body: JSON.stringify({ content }) }),
  deleteMessage:          (index) => request(`/api/session/messages/${index}`, { method: 'DELETE' }),
  deleteSave:             (saveId) => request(`/api/saves/${saveId}`, { method: 'DELETE' }),
  renameSave:             (saveId, displayName) => request(`/api/saves/${saveId}/name`, { method: 'PUT', body: JSON.stringify({ display_name: displayName }) }),
  getStoryStyle:          (saveId) => request(`/api/saves/${saveId}/story-style`),
  setStoryStyle:          (saveId, style) => request(`/api/saves/${saveId}/story-style`, { method: 'PUT', body: JSON.stringify(style) }),
  branchSave:             (saveId, opts = {}) => request(`/api/saves/${saveId}/branch`, { method: 'POST', body: JSON.stringify({
                            new_save_id: opts.newSaveId ?? null,
                            target_turn: opts.targetTurn ?? null,
                            display_name: opts.displayName ?? null,
                          }) }),
  // Direct download URL (used as an anchor href, not fetched as JSON).
  exportSaveUrl:          (saveId, format = 'md') => `${API}/api/saves/${saveId}/export?format=${encodeURIComponent(format)}`,
  getSaveActiveModules:   (saveId) => request(`/api/saves/${saveId}/active-modules`),
  setSaveActiveModules:   (saveId, activeModules) => request(`/api/saves/${saveId}/active-modules`, { method: 'PUT', body: JSON.stringify({ active_modules: activeModules }) }),
  // Per-module instruction-slot overrides (customizable LLM directives)
  getInstructionSlots:        (modId) => request(`/api/modules/${modId}/instruction-slots`),
  getSaveModuleInstructions:  (saveId) => request(`/api/saves/${saveId}/module-instructions`),
  setSaveModuleInstructions:  (saveId, instructions) => request(`/api/saves/${saveId}/module-instructions`, { method: 'PUT', body: JSON.stringify({ module_instructions: instructions }) }),
  rewriteModuleInstruction:   (modId, slotId, { request: req, currentText = null, scenarioContext = null }) =>
    request(`/api/modules/${modId}/instructions/${slotId}/rewrite`, { method: 'POST', body: JSON.stringify({ request: req, current_text: currentText, scenario_context: scenarioContext }) }),
  // One request, every slot of the module: a single LLM call rewrites each
  // slot the request concerns and skips the rest ({instructions: {slotId: text}}).
  rewriteAllModuleInstructions: (modId, { request: req, current = null, scenarioContext = null }) =>
    request(`/api/modules/${modId}/instructions/rewrite-all`, { method: 'POST', body: JSON.stringify({ request: req, current, scenario_context: scenarioContext }) }),
  getModules:             () => request('/api/modules'),
  getModuleConfigs:       () => request('/api/session/module-configs'),
  updateModuleConfigs:    (configs) => request('/api/session/module-configs', { method: 'PUT', body: JSON.stringify({ module_configs: configs }) }),
  getPromptPipeline:      () => request('/api/session/prompt-pipeline'),
  updatePromptPipeline:   (pipeline) => request('/api/session/prompt-pipeline', { method: 'PUT', body: JSON.stringify({ prompt_pipeline: pipeline }) }),
  previewPromptPipeline:  (pipeline) => request('/api/session/prompt-pipeline/preview', { method: 'POST', body: JSON.stringify({ prompt_pipeline: pipeline }) }),
  resetPromptPipeline:    () => request('/api/session/prompt-pipeline/reset', { method: 'POST' }),

  // Prompt library
  getPromptTemplates:     (category) => request(`/api/prompts${category ? `?category=${encodeURIComponent(category)}` : ''}`),
  createPromptTemplate:   (name, config, category = 'other') => request('/api/prompts', { method: 'POST', body: JSON.stringify({ name, config, category }) }),
  updatePromptTemplate:   (templateId, patch) => request(`/api/prompts/${templateId}`, { method: 'PUT', body: JSON.stringify(patch) }),
  deletePromptTemplate:   (templateId) => request(`/api/prompts/${templateId}`, { method: 'DELETE' }),
  templateToBlock:        (templateId, blockId) => request(`/api/prompts/${templateId}/to-block`, { method: 'POST', body: JSON.stringify(blockId ? { block_id: blockId } : {}) }),
  getPromptMacros:        () => request('/api/prompts/macros'),
  getDefaultBlocks:       () => request('/api/prompts/defaults'),
  importSillyTavernPreset: (data) => request('/api/prompts/import-sillytavern', { method: 'POST', body: JSON.stringify(data) }),

  // Global prompt pipeline
  getGlobalPromptPipeline:    () => request('/api/global-prompt-pipeline'),
  updateGlobalPromptPipeline: (pipeline) => request('/api/global-prompt-pipeline', { method: 'PUT', body: JSON.stringify({ prompt_pipeline: pipeline }) }),
  resetGlobalPromptPipeline:  () => request('/api/global-prompt-pipeline/reset', { method: 'POST' }),

  // Continue prompt (injected as the user turn on an empty send)
  getContinuePrompt:    () => request('/api/continue-prompt'),
  updateContinuePrompt: (text) => request('/api/continue-prompt', { method: 'PUT', body: JSON.stringify({ text }) }),
  resetContinuePrompt:  () => request('/api/continue-prompt/reset', { method: 'POST' }),

  // UI theme (global, server-persisted)
  getTheme:                   () => request('/api/theme'),
  updateTheme:                (theme) => request('/api/theme', { method: 'PUT', body: JSON.stringify(theme) }),
  getHealth:              () => request('/api/health'),
  getMemories:            () => request('/api/session/memories'),
  getMemoryContext:       () => request('/api/session/memories/context'),
  deleteMemory:           (id) => request(`/api/session/memories/${id}`, { method: 'DELETE' }),
  updateMemory:           (id, patch) => request(`/api/session/memories/${id}`, { method: 'PUT', body: JSON.stringify(patch) }),
  ragDebugQuery:          (query, limit = 10) => request('/api/session/memories/rag-debug', { method: 'POST', body: JSON.stringify({ query, limit }) }),
  getWorldEntries:        () => request('/api/session/world-entries'),
  updateWorldEntry:       (id, text) => request(`/api/session/world-entries/${id}`, { method: 'PUT', body: JSON.stringify({ text }) }),
  getLLMInspectorCalls:   (sinceId = '', limit = 50) => {
    const qs = sinceId ? `?since_id=${encodeURIComponent(sinceId)}&limit=${limit}` : `?limit=${limit}`;
    return request(`/api/llm-inspector/calls${qs}`);
  },
  clearLLMInspectorCalls: () => request('/api/llm-inspector/calls', { method: 'DELETE' }),
  getServerLogs:          (sinceId = 0, level = '', limit = 1000) => {
    const params = new URLSearchParams({ limit });
    if (sinceId) params.set('since_id', sinceId);
    if (level) params.set('level', level);
    return request(`/api/logs?${params}`);
  },
  clearServerLogs:        () => request('/api/logs', { method: 'DELETE' }),
  // Debug dumps (direct download URLs, used as anchor hrefs).
  llmLogDumpUrl:          () => `${API}/api/llm-log/dump`,
  saveDumpUrl:            (saveId) => `${API}/api/saves/${saveId}/dump`,
  getSettings:            (scope = 'story') => request(`/api/settings?scope=${scope}`),
  updateSettings:         (updates, scope = 'story') => request('/api/settings', { method: 'PUT', body: JSON.stringify({ settings: updates, scope }) }),
  getWidget:              (modId) => fetch(`${API}/widgets/${modId}/widget.jsx?_ts=${Date.now()}`),

  getWidgetFile:          (modId, filename) => fetch(`${API}/widgets/${modId}/${filename}?_ts=${Date.now()}`),
  // Provider management
  getProviders:           () => request('/api/providers'),
  getActiveProvider:      () => request('/api/providers/active'),
  setActiveProvider:      (providerId) => request('/api/providers/active', { method: 'PUT', body: JSON.stringify({ provider_id: providerId }) }),
  getProviderConfig:      (id) => request(`/api/providers/${id}/config`),
  updateProviderConfig:   (id, config) => request(`/api/providers/${id}/config`, { method: 'PUT', body: JSON.stringify({ config }) }),
  testProvider:           (id) => request(`/api/providers/${id}/test`, { method: 'POST' }),
  fetchProviderModels:    (id) => request(`/api/providers/${id}/models`),
  applyProviderPreset:    (id, preset) => request(`/api/providers/${id}/preset`, { method: 'POST', body: JSON.stringify({ preset }) }),

  // Scenarios (basic story source)
  listScenarios:          () => request('/api/scenarios'),
  loadScenario:           (scenarioId) => request(`/api/scenarios/${scenarioId}`),
  saveScenario:           (data) => request('/api/scenarios', { method: 'POST', body: JSON.stringify(data) }),
  deleteScenario:         (scenarioId) => request(`/api/scenarios/${scenarioId}`, { method: 'DELETE' }),
  // LLM-as-editor: rewrite a scenario's starting_prompt/scenario_description to
  // fit a natural-language request. `field` picks which prose field is edited.
  rewriteScenarioPrompt:  ({ request: req, currentText = null, field = 'starting_prompt', context = {} }) =>
                            request('/api/scenarios/rewrite-prompt', { method: 'POST', body: JSON.stringify({
                              request: req, current_text: currentText, field,
                              name: context.name ?? null,
                              scenario_description: context.scenario_description ?? null,
                              starting_prompt: context.starting_prompt ?? null,
                              themes: context.themes ?? null,
                              tags: context.tags ?? null,
                              pacing: context.pacing ?? null,
                            }) }),

  // Lorebooks (SillyTavern World Info imports, RAG-retrieved lore)
  importLorebook:          (data, name = null) => request('/api/lorebooks/import', { method: 'POST', body: JSON.stringify({ data, name }) }),
  listLorebooks:           () => request('/api/lorebooks'),
  getLorebook:             (lorebookId) => request(`/api/lorebooks/${lorebookId}`),
  updateLorebook:          (lorebookId, patch) => request(`/api/lorebooks/${lorebookId}`, { method: 'PUT', body: JSON.stringify(patch) }),
  deleteLorebook:          (lorebookId) => request(`/api/lorebooks/${lorebookId}`, { method: 'DELETE' }),
  setLorebookEntryEnabled: (lorebookId, uid, enabled) => request(`/api/lorebooks/${lorebookId}/entries/${encodeURIComponent(uid)}`, { method: 'PUT', body: JSON.stringify({ enabled }) }),
  updateLorebookEntry:     (lorebookId, uid, patch) => request(`/api/lorebooks/${lorebookId}/entries/${encodeURIComponent(uid)}`, { method: 'PUT', body: JSON.stringify(patch) }),
  getLorebookLinks:        (kind, targetId) => request(`/api/lorebooks/links/${kind}/${targetId}`),
  setLorebookLinks:        (kind, targetId, lorebookIds) => request(`/api/lorebooks/links/${kind}/${targetId}`, { method: 'PUT', body: JSON.stringify({ lorebook_ids: lorebookIds }) }),
  getSaveLorebooks:        (saveId) => request(`/api/saves/${saveId}/lorebooks`),
  setSaveLorebooks:        (saveId, lorebookIds) => request(`/api/saves/${saveId}/lorebooks`, { method: 'PUT', body: JSON.stringify({ lorebook_ids: lorebookIds }) }),
  // Free-standing entries owned by one save (not part of any imported book)
  addStoryLorebookEntry:    (saveId, entry) => request(`/api/saves/${saveId}/lorebooks/entries`, { method: 'POST', body: JSON.stringify(entry) }),
  updateStoryLorebookEntry: (saveId, uid, patch) => request(`/api/saves/${saveId}/lorebooks/entries/${encodeURIComponent(uid)}`, { method: 'PUT', body: JSON.stringify(patch) }),
  deleteStoryLorebookEntry: (saveId, uid) => request(`/api/saves/${saveId}/lorebooks/entries/${encodeURIComponent(uid)}`, { method: 'DELETE' }),

  // World Builder
  getWorldPipeline:       () => request('/api/world/pipeline'),
  generateWorld:          (seedPrompt, skipReview = false, scenarioId = null) => request('/api/world/generate', { method: 'POST', body: JSON.stringify({ seed_prompt: seedPrompt, skip_review: skipReview, ...(scenarioId ? { scenario_id: scenarioId } : {}) }) }),
  // LLM-as-author: write/rewrite the World Prompt from the player's notes
  // (instruction), the current draft, and an optional linked scenario.
  rewriteWorldPrompt:     ({ instruction = '', currentText = null, scenarioId = null }) => request('/api/world/rewrite-prompt', { method: 'POST', body: JSON.stringify({ instruction, current_text: currentText, scenario_id: scenarioId }) }),
  // Ideation conversation (C4): one stateless turn — the model answers the
  // player and returns the updated seed-prompt + world-rules drafts, plus
  // `ready` (its judgment that the idea is settled — the go offer). The
  // client holds the conversation and the drafts round-trip every turn.
  ideationTurn:           ({ messages = [], prompt = null, rules = [], scenarioId = null }) => request('/api/world/ideation-turn', { method: 'POST', body: JSON.stringify({ messages, prompt, rules, scenario_id: scenarioId }) }),
  generateWorldStep:      (stepId, note = '', data = null) => {
    const body = { note };
    if (data) body.data = data;
    return request(`/api/world/generate-step/${stepId}`, { method: 'POST', body: JSON.stringify(body) });
  },
  approveWorldStep:       (stepId, data = null) => request(`/api/world/approve-step/${stepId}`, { method: 'POST', body: JSON.stringify(data ? { data } : {}) }),
  regenerateWorldItem:    (stepId, field, index, items, note = '', subfield = null) => request(`/api/world/regenerate-item/${stepId}`, { method: 'POST', body: JSON.stringify({ field, index, items, note, subfield }) }),
  getWorldState:          () => request('/api/world/state'),
  // Re-enter an interrupted one-shot generation (backend was killed mid-run
  // while the app was minimized); no-op if it's running, complete, or review-mode.
  continueWorldGeneration: () => request('/api/world/continue', { method: 'POST' }),
  // Agent builds (C2): a server-side agent plans, builds and verifies the
  // whole world on its own, from the ideation brief (seed prompt + the
  // co-authored world rules). Launch returns the new world id immediately;
  // poll status (or stream the events endpoint) to watch, cancel any time.
  agentBuild:             (seedPrompt, scenarioId = null, rules = []) => request('/api/world/agent/build', { method: 'POST', body: JSON.stringify({ seed_prompt: seedPrompt, rules, ...(scenarioId ? { scenario_id: scenarioId } : {}) }) }),
  agentBuildStatus:       (worldId) => request(`/api/world/${worldId}/agent/status`),
  agentBuildCancel:       (worldId) => request(`/api/world/${worldId}/agent/cancel`, { method: 'POST' }),
  // Agent build event stream: replays the persisted action log from `after`
  // (persisted events carry their index `i`), then streams live — including
  // transient enrichment progress events (no `i`). Every event is handed to
  // onEvent; resolves with the terminal {type:"done"} event, or null when
  // the stream dropped before one arrived (reconnect with the last seen
  // i + 1). Pass an AbortController signal to detach without cancelling
  // the build.
  agentBuildEvents: async (worldId, { after = 0 } = {}, onEvent, signal) => {
    const res = await fetch(`${API}/api/world/${worldId}/agent/events`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ after }),
      signal,
    });
    if (!res.ok || !res.body) {
      const err = await res.json().catch(() => ({}));
      throw new ApiError(res.status, err.detail || res.statusText);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let final = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        const dataLine = block.split('\n').find((l) => l.startsWith('data:'));
        if (!dataLine) continue;
        const data = JSON.parse(dataLine.slice(5).trim());
        if (data.type === 'done') final = data;
        onEvent?.(data);
      }
    }
    return final;
  },
  compileWorld:           (saveId = null) => request('/api/world/compile', { method: 'POST', body: JSON.stringify(saveId ? { save_id: saveId } : {}) }),
  saveWorld:              (worldId) => request('/api/world/save', { method: 'POST', body: JSON.stringify({ world_id: worldId }) }),
  discardWorld:           () => request('/api/world/discard', { method: 'POST' }),
  listWorlds:             () => request('/api/world/list'),
  loadWorld:              (worldId) => request(`/api/world/load/${worldId}`),
  resumeWorld:            (worldId) => request('/api/world/resume', { method: 'POST', body: JSON.stringify({ world_id: worldId }) }),
  deleteWorld:            (worldId) => request(`/api/world/${worldId}`, { method: 'DELETE' }),
  saveWorldStep:          (worldId, stepId, data) => request(`/api/world/save-step/${worldId}/${stepId}`, { method: 'POST', body: JSON.stringify({ data }) }),
  getStartLocations:      (worldId) => request(`/api/world/${worldId}/start-locations`),
  pickStartLocation:      (worldId, preference = '') => request(`/api/world/${worldId}/pick-start`, { method: 'POST', body: JSON.stringify({ preference }) }),
  enrichPasses:           (worldId) => request(`/api/world/${worldId}/enrich/passes`),
  enrichProgress:         (worldId, layerId = null) => {
    const qs = layerId ? `?layer_id=${encodeURIComponent(layerId)}` : '';
    return request(`/api/world/${worldId}/enrich/progress${qs}`);
  },
  // Server-driven enrichment run: one POST that streams SSE progress events
  // ({type:"phase"|"node"|"failed"}) and resolves with the terminal
  // {type:"done"} summary. Pass an AbortController signal to stop mid-run.
  enrichRun: async (worldId, { phase = 'all', count = null, layerId = null, rework = false, excludeNodeIds = null } = {}, onEvent, signal) => {
    const body = { phase, rework };
    if (count) body.count = count;
    if (layerId) body.layer_id = layerId;
    if (excludeNodeIds?.length) body.exclude_node_ids = excludeNodeIds;
    const res = await fetch(`${API}/api/world/${worldId}/enrich/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok || !res.body) {
      const err = await res.json().catch(() => ({}));
      throw new ApiError(res.status, err.detail || res.statusText);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let final = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        const dataLine = block.split('\n').find((l) => l.startsWith('data:'));
        if (!dataLine) continue;
        const data = JSON.parse(dataLine.slice(5).trim());
        if (data.type === 'error') throw new ApiError(500, data.detail || 'enrichment run failed');
        if (data.type === 'done') final = data;
        else onEvent?.(data);
      }
    }
    if (!final) throw new ApiError(500, 'enrichment stream ended without a result');
    return final;
  },
  enrichCancel:           (worldId) => request(`/api/world/${worldId}/enrich/cancel`, { method: 'POST' }),
  // Site interiors (lazy per-location detail)
  expandWorldSite:        (worldId, nodeId, force = false) => request(`/api/world/${worldId}/site/${nodeId}/expand`, { method: 'POST', body: JSON.stringify({ force }) }),
  getWorldSites:          (worldId) => request(`/api/world/${worldId}/sites`),
  expandSessionSite:      (nodeId) => request('/api/world/session/expand-site', { method: 'POST', body: JSON.stringify({ node_id: nodeId }) }),
  enrichCommit:           (worldId, stepId) => request(`/api/world/${worldId}/enrich/commit`, { method: 'POST', body: JSON.stringify({ step_id: stepId }) }),
  // Debug / seed
  debugSeedWorld:         (seedPrompt, worldId = null, totalNodes = 60) => request('/api/world/debug/seed', { method: 'POST', body: JSON.stringify({ seed_prompt: seedPrompt, world_id: worldId, total_nodes: totalNodes }) }),
  debugSkipTo:            (stepId, worldId, totalNodes = 60) => request(`/api/world/debug/skip-to/${stepId}`, { method: 'POST', body: JSON.stringify({ world_id: worldId, total_nodes: totalNodes }) }),

  // Fog of War
  revealMapNode:          (nodeId) => request('/api/session/reveal-node', { method: 'POST', body: JSON.stringify({ node_id: nodeId }) }),

  // Character Builder
  listCharacters:           () => request('/api/character/list'),
  generateCharacterName:    (params) => request('/api/character/generate-name', { method: 'POST', body: JSON.stringify(params) }),
  generateCharacterAppearance: (params) => request('/api/character/generate-appearance', { method: 'POST', body: JSON.stringify(params) }),
  saveCharacter:            (data) => request('/api/character/save', { method: 'POST', body: JSON.stringify(data) }),
  loadCharacter:            (id) => request(`/api/character/load/${id}`),
  deleteCharacter:          (id) => request(`/api/character/${id}`, { method: 'DELETE' }),
  getCharacterModuleDefaults: (context = {}) =>
    request('/api/character/module-defaults', { method: 'POST', body: JSON.stringify({ context }) }),
  generateCharacterRace: (params) => request('/api/character/generate-race', { method: 'POST', body: JSON.stringify(params) }),
  generateCharacterStats: (params) => request('/api/character/generate-stats', { method: 'POST', body: JSON.stringify(params) }),

  // Experimental terrain visualization
  generateTerrain:          (params) => request('/api/terrain/generate', { method: 'POST', body: JSON.stringify(params) }),

  // Underground/cave generation (single-shot; caves carve fast, no streaming).
  generateCaveTerrain:      (params) => request('/api/terrain/cave/generate', { method: 'POST', body: JSON.stringify(params) }),

  // Editable biome palettes (realistic + fantasy) for the colour editor.
  getTerrainPalette:        () => request('/api/terrain/palette'),

  // Fast biome-only re-derive on an already-generated run (no erosion/rivers).
  rederiveBiomes:           (runId, params) => request(`/api/terrain/${runId}/biomes`, { method: 'POST', body: JSON.stringify(params) }),

  // Streaming variant: invokes onFrame(frame) for each work-in-progress preview
  // and resolves with the final payload (run_id + full-res image URLs). Parses
  // Server-Sent Events off a POST body (EventSource can't POST params).
  generateTerrainStream: async (params, onFrame) => {
    const res = await fetch(`${API}/api/terrain/generate/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    if (!res.ok || !res.body) {
      const err = await res.json().catch(() => ({}));
      throw new ApiError(res.status, err.detail || res.statusText);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let final = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        const dataLine = block.split('\n').find((l) => l.startsWith('data:'));
        if (!dataLine) continue;
        const data = JSON.parse(dataLine.slice(5).trim());
        if (data.type === 'frame') onFrame?.(data);
        else if (data.type === 'done') final = data;
        else if (data.type === 'error') throw new ApiError(500, data.detail || 'generation failed');
      }
    }
    if (!final) throw new ApiError(500, 'stream ended without a result');
    return final;
  },
};
