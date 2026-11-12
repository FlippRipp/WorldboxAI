import { useState, useEffect, useRef } from 'react';
import WidgetErrorBoundary from '../shared/WidgetErrorBoundary';
import SkeletonLoader from '../shared/SkeletonLoader';
import { loadModuleComponent } from '../shared/moduleLoader';

const widgetCache = new Map();

function WidgetRenderer({ componentRef, value, onChange, context, modId }) {
  const Comp = componentRef.current;
  if (!Comp) return null;
  return (
    <WidgetErrorBoundary modId={modId}>
      <Comp value={value} onChange={onChange} context={context} />
    </WidgetErrorBoundary>
  );
}

export default function CharacterModuleForm({ modId, value, onChange, context }) {
  const componentRef = useRef(widgetCache.get(`char_${modId}`) || null);
  const [loaded, setLoaded] = useState(widgetCache.has(`char_${modId}`));
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    const cacheKey = `char_${modId}`;

    if (widgetCache.has(cacheKey)) {
      componentRef.current = widgetCache.get(cacheKey);
      if (!loaded) setLoaded(true);
      return;
    }

    let cancelled = false;

    loadModuleComponent(modId, 'character_widget.jsx')
      .then(Comp => {
        if (cancelled || !mountedRef.current) return;
        widgetCache.set(cacheKey, Comp);
        componentRef.current = Comp;
        setLoaded(true);
      })
      .catch(err => {
        if (err?.status !== 404) {
          console.error(`Error loading character widget for ${modId}:`, err);
        }
        if (!cancelled && mountedRef.current) setLoaded(true);
      });

    return () => { cancelled = true; mountedRef.current = false; };
  }, [modId, loaded]);

  if (!loaded) return <SkeletonLoader />;

  return (
    <WidgetRenderer
      componentRef={componentRef}
      value={value}
      onChange={onChange}
      context={context}
      modId={modId}
    />
  );
}
