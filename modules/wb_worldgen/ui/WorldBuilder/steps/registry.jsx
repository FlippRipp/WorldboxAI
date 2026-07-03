import SchemaForm from './SchemaForm';
import MapStepView from './MapStepView';
import EnrichmentStepView from './EnrichmentStepView';
import TerrainStepView from './TerrainStepView';

/**
 * Step UI registry.
 *
 * Maps a backend step id to the React component that renders its body plus
 * which action-bar controls apply. This mirrors the backend's modular step
 * registry: to give a step custom UI, add one entry here; any step without
 * an entry falls back to the schema-driven form. Removing a step is just
 * deleting its entry (and its backend module).
 *
 * Descriptor shape:
 *   {
 *     component: React component (receives the step body context),
 *     actions: { reroll: bool, edit: bool }
 *   }
 */
const STEP_UI_REGISTRY = {
  terrain_generation: {
    component: TerrainStepView,
    actions: { reroll: true, edit: false },
  },
  map_generation: {
    component: MapStepView,
    actions: { reroll: true, edit: true },
  },
  node_labeling: {
    component: EnrichmentStepView,
    actions: { reroll: false, edit: false },
  },
  node_descriptions: {
    component: EnrichmentStepView,
    actions: { reroll: false, edit: false },
  },
};

const DEFAULT_DESCRIPTOR = {
  component: SchemaForm,
  actions: { reroll: true, edit: true },
};

export function getStepDescriptor(stepId) {
  return STEP_UI_REGISTRY[stepId] || DEFAULT_DESCRIPTOR;
}

export default STEP_UI_REGISTRY;
