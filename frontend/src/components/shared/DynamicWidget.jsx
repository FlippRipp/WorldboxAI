import React, { useState, useEffect, useRef } from 'react';
import WidgetErrorBoundary from './WidgetErrorBoundary';
import SkeletonLoader from './SkeletonLoader';
import { loadModuleComponent } from './moduleLoader';

// Renders a module's slot widget (default entry: widget.jsx). Loading,
// dependency resolution, and multi-file support are handled by the shared
// module loader; this component just wires the result into a slot.
export default function DynamicWidget({ modId, entryFile = 'widget.jsx', state, config, slotName, assetsBaseUrl, eventBus, slotProps, skeleton = true }) {
  const [Comp, setComp] = useState(null);
  const [resolved, setResolved] = useState(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    setResolved(false);
    loadModuleComponent(modId, entryFile)
      .then((C) => {
        if (!mountedRef.current) return;
        setComp(() => C);
        setResolved(true);
      })
      .catch((err) => {
        console.error(`[DynamicWidget] ${modId}/${entryFile} error:`, err);
        if (mountedRef.current) setResolved(true);
      });
    return () => { mountedRef.current = false; };
  }, [modId, entryFile]);

  if (!resolved) return skeleton ? <SkeletonLoader /> : null;
  if (!Comp) return null;

  return (
    <WidgetErrorBoundary modId={modId}>
      <Comp state={state} config={config} slotName={slotName} assetsBaseUrl={assetsBaseUrl} eventBus={eventBus} {...slotProps} />
    </WidgetErrorBoundary>
  );
}
