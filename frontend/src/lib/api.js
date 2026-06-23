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
  createSave:             (saveId, worldId = null, startPreference = null, characterId = null) => request('/api/saves', { method: 'POST', body: JSON.stringify({ save_id: saveId, world_id: worldId, start_preference: startPreference, character_id: characterId }) }),
  loadSave:               (saveId) => request(`/api/saves/${saveId}/load`, { method: 'POST' }),
  undoSave:               (saveId, targetTurn) => request(`/api/saves/${saveId}/undo`, { method: 'POST', body: JSON.stringify({ target_turn: targetTurn }) }),
  deleteSave:             (saveId) => request(`/api/saves/${saveId}`, { method: 'DELETE' }),
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
  getHealth:              () => request('/api/health'),
  getMemories:            () => request('/api/session/memories'),
  getMemoryContext:       () => request('/api/session/memories/context'),
  deleteMemory:           (id) => request(`/api/session/memories/${id}`, { method: 'DELETE' }),
  getLLMInspectorCalls:   (sinceId = '', limit = 50) => {
    const qs = sinceId ? `?since_id=${encodeURIComponent(sinceId)}&limit=${limit}` : `?limit=${limit}`;
    return request(`/api/llm-inspector/calls${qs}`);
  },
  clearLLMInspectorCalls: () => request('/api/llm-inspector/calls', { method: 'DELETE' }),
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

  // World Builder
  getWorldPipeline:       () => request('/api/world/pipeline'),
  generateWorld:          (seedPrompt, skipReview = false) => request('/api/world/generate', { method: 'POST', body: JSON.stringify({ seed_prompt: seedPrompt, skip_review: skipReview }) }),
  generateWorldStep:      (stepId, note = '', data = null) => {
    const body = { note };
    if (data) body.data = data;
    return request(`/api/world/generate-step/${stepId}`, { method: 'POST', body: JSON.stringify(body) });
  },
  approveWorldStep:       (stepId, data = null) => request(`/api/world/approve-step/${stepId}`, { method: 'POST', body: JSON.stringify(data ? { data } : {}) }),
  regenerateWorldItem:    (stepId, field, index, items, note = '') => request(`/api/world/regenerate-item/${stepId}`, { method: 'POST', body: JSON.stringify({ field, index, items, note }) }),
  getWorldState:          () => request('/api/world/state'),
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
  enrichLabelNext:        (worldId, layerId = null, labeledNodeIds = null) => {
    const body = {};
    if (layerId) body.layer_id = layerId;
    if (labeledNodeIds) body.labeled_node_ids = labeledNodeIds;
    return request(`/api/world/${worldId}/enrich/label-next`, { method: 'POST', body: JSON.stringify(body) });
  },
  enrichDescribeNext:     (worldId, layerId = null, labeledNodeIds = null) => {
    const body = {};
    if (layerId) body.layer_id = layerId;
    if (labeledNodeIds) body.labeled_node_ids = labeledNodeIds;
    return request(`/api/world/${worldId}/enrich/describe-next`, { method: 'POST', body: JSON.stringify(body) });
  },
  enrichProgress:         (worldId, layerId = null) => {
    const qs = layerId ? `?layer_id=${encodeURIComponent(layerId)}` : '';
    return request(`/api/world/${worldId}/enrich/progress${qs}`);
  },
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
  getCharacterModuleDefaults: (worldId) => {
    const qs = worldId ? `?world_id=${encodeURIComponent(worldId)}` : '';
    return request(`/api/character/module-defaults${qs}`);
  },
  generateCharacterRace: (params) => request('/api/character/generate-race', { method: 'POST', body: JSON.stringify(params) }),
  generateCharacterStats: (params) => request('/api/character/generate-stats', { method: 'POST', body: JSON.stringify(params) }),
};
