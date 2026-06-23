import { useState, useEffect, useCallback } from 'react';
import { api } from '../lib/api';

export function useModules() {
  const [modules, setModules] = useState([]);

  useEffect(() => {
    api.getModules()
      .then(data => setModules(data.modules || []))
      .catch(err => console.error('Failed to load modules:', err));
  }, []);

  return { modules, setModules };
}

export function usePromptPipeline(sessionState, onUpdate, onPreview) {
  const [pipeline, setPipeline] = useState([]);
  const [trace, setTrace] = useState([]);

  useEffect(() => {
    if (sessionState) {
      setPipeline(sessionState.prompt_pipeline || []);
      setTrace(sessionState.last_prompt_trace || []);
    }
  }, [sessionState?.prompt_pipeline, sessionState?.last_prompt_trace]);

  const savePipeline = useCallback(async (nextPipeline) => {
    if (onUpdate) {
      const data = await onUpdate(nextPipeline);
      setPipeline(data.prompt_pipeline || []);
      return data;
    }
    const data = await api.updatePromptPipeline(nextPipeline);
    setPipeline(data.prompt_pipeline || []);
    return data;
  }, [onUpdate]);

  const previewPipeline = useCallback(async (nextPipeline) => {
    if (onPreview) return onPreview(nextPipeline);
    return api.previewPromptPipeline(nextPipeline);
  }, [onPreview]);

  return { pipeline, trace, savePipeline, previewPipeline, setTrace };
}
