import { useState, useEffect } from 'react';
import { loadModuleComponent } from './moduleLoader';
import WidgetErrorBoundary from './WidgetErrorBoundary';

// Loads a module's declared `game_overlay` component and renders it over the
// game view, passing the whole game `state`. The overlay decides whether to
// show anything (e.g. wb_worldgen's map renders only when world_data exists),
// so the host stays world-agnostic.
export default function ModuleGameOverlay({ modId, file, state }) {
  const [Comp, setComp] = useState(null);

  useEffect(() => {
    let alive = true;
    loadModuleComponent(modId, file)
      .then((c) => { if (alive) setComp(() => c); })
      .catch(() => { if (alive) setComp(null); });
    return () => { alive = false; };
  }, [modId, file]);

  if (!Comp) return null;
  return (
    <WidgetErrorBoundary modId={modId}>
      <Comp state={state} />
    </WidgetErrorBoundary>
  );
}
