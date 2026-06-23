import { useState, useCallback, useRef } from 'react';
import { api } from '../lib/api';

export function useSession() {
  const [sessionState, setSessionState] = useState(null);
  const [saves, setSaves] = useState([]);
  const [moduleConfigs, setModuleConfigs] = useState({});
  const [promptPipeline, setPromptPipeline] = useState([]);
  const [promptTrace, setPromptTrace] = useState([]);
  const [loading, setLoading] = useState(false);
  const abortRef = useRef(null);

  const refreshSession = useCallback(async () => {
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    const { signal } = abortRef.current;

    setLoading(true);
    try {
      const [sessionData, savesData, configsData, promptData] = await Promise.all([
        makeRequest('/api/session', signal),
        makeRequest('/api/saves', signal),
        makeRequest('/api/session/module-configs', signal),
        makeRequest('/api/session/prompt-pipeline', signal),
      ]);

      if (!signal.aborted) {
        setSessionState(sessionData);
        setSaves(savesData.saves || []);
        setModuleConfigs(configsData.module_configs || {});
        setPromptPipeline(promptData.prompt_pipeline || []);
        setPromptTrace(promptData.last_prompt_trace || []);
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        console.error('Session refresh failed:', e);
      }
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, []);

  async function makeRequest(path, signal) {
    const res = await fetch(path, { signal, headers: { 'Content-Type': 'application/json' } });
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json();
  }

  const createSave = useCallback(async (saveId) => {
    const data = await api.createSave(saveId);
    setSessionState(data.session);
    await refreshSession();
    return data;
  }, [refreshSession]);

  const loadSave = useCallback(async (saveId) => {
    const data = await api.loadSave(saveId);
    setSessionState(data.session);
    await refreshSession();
    return data;
  }, [refreshSession]);

  const undoTurn = useCallback(async (targetTurn) => {
    const saveId = sessionState?.active_save_id;
    if (!saveId) return;
    const data = await api.undoSave(saveId, targetTurn);
    setSessionState(data.session);
    await refreshSession();
    return data;
  }, [sessionState, refreshSession]);

  const updateModuleConfigs = useCallback(async (configs) => {
    const data = await api.updateModuleConfigs(configs);
    setModuleConfigs(data.module_configs || {});
    setSessionState(data.session);
    return data;
  }, []);

  const updatePromptPipeline = useCallback(async (pipeline) => {
    const data = await api.updatePromptPipeline(pipeline);
    setPromptPipeline(data.prompt_pipeline || []);
    setSessionState(data.session);
    return data;
  }, []);

  const previewPromptPipeline = useCallback(async (pipeline) => {
    return api.previewPromptPipeline(pipeline);
  }, []);

  const applyLoadedState = useCallback((state) => {
    setModuleConfigs(state.module_configs || {});
    setPromptPipeline(state.prompt_pipeline || []);
    setPromptTrace(state.last_prompt_trace || []);
    return {
      messages: state.chat_messages || (state.history || []).map(content => ({ role: 'ai', content }))
    };
  }, []);

  return {
    sessionState, saves, moduleConfigs, promptPipeline, promptTrace, loading,
    refreshSession, createSave, loadSave, undoTurn,
    updateModuleConfigs, updatePromptPipeline, previewPromptPipeline,
    applyLoadedState, setSessionState, setPromptTrace, setSaves
  };
}

class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}
