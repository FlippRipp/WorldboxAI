import React, { useState, useEffect, useRef } from 'react';
import WidgetErrorBoundary from './WidgetErrorBoundary';
import { loadModuleComponent } from './moduleLoader';

// Renders a module-supplied full-screen view (manifest `modes[].screen` or
// `storyteller_start.screen`). The loaded component receives the same props a
// core screen would: onBack plus any extra context the host passes through.
export default function ModuleScreen({ modId, screen, onBack, ...rest }) {
  const [Comp, setComp] = useState(null);
  const [error, setError] = useState(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    setComp(null);
    setError(null);
    loadModuleComponent(modId, screen)
      .then((C) => { if (mountedRef.current) setComp(() => C); })
      .catch((err) => {
        console.error(`[ModuleScreen] ${modId}/${screen} failed to load:`, err);
        if (mountedRef.current) setError(err);
      });
    return () => { mountedRef.current = false; };
  }, [modId, screen]);

  if (error) {
    return (
      <div className="min-h-screen bg-gray-950 text-gray-100 flex flex-col items-center justify-center gap-4 p-6">
        <p className="text-red-400">Failed to load module screen: {error.message}</p>
        {onBack && (
          <button onClick={onBack} className="px-4 py-2 rounded bg-gray-800 hover:bg-gray-700">
            Back
          </button>
        )}
      </div>
    );
  }

  if (!Comp) {
    // Full-screen loading state matching the main-menu background so there's no
    // white flash while the module's JSX is fetched + compiled at runtime.
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex items-center justify-center">
        <div className="h-8 w-8 rounded-full border-2 border-gray-700 border-t-purple-400 animate-spin" />
      </div>
    );
  }

  return (
    <WidgetErrorBoundary modId={modId}>
      <Comp onBack={onBack} {...rest} />
    </WidgetErrorBoundary>
  );
}
