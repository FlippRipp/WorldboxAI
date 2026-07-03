import { useState, useEffect } from 'react';
import { loadModuleComponent } from './moduleLoader';
import WidgetErrorBoundary from './WidgetErrorBoundary';

// Loads a module-supplied component and renders it inline (not full-screen),
// forwarding arbitrary props. Used to embed module-contributed UI such as the
// storyteller-start story-source picker into a host screen.
export default function ModuleInline({ modId, file, fallback = null, ...props }) {
  const [Comp, setComp] = useState(null);

  useEffect(() => {
    let alive = true;
    loadModuleComponent(modId, file)
      .then((c) => { if (alive) setComp(() => c); })
      .catch(() => { if (alive) setComp(null); });
    return () => { alive = false; };
  }, [modId, file]);

  if (!Comp) return fallback;
  return (
    <WidgetErrorBoundary modId={modId}>
      <Comp {...props} />
    </WidgetErrorBoundary>
  );
}
