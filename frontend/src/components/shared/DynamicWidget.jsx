import React, { useState, useEffect, useRef } from 'react';
import * as Babel from '@babel/standalone';
import WidgetErrorBoundary from './WidgetErrorBoundary';
import SkeletonLoader from './SkeletonLoader';
import { api } from '../../lib/api';

const widgetCache = new Map();

function requireMock(moduleName) {
  if (moduleName === 'react') return React;
  throw new Error(`Module "${moduleName}" not available to widgets`);
}

function WidgetRenderer({ componentRef, modId, state, config, slotName, assetsBaseUrl, eventBus }) {
  const Comp = componentRef.current;
  if (!Comp) return null;
  return (
    <WidgetErrorBoundary modId={modId}>
      <Comp state={state} config={config} slotName={slotName} assetsBaseUrl={assetsBaseUrl} eventBus={eventBus} />
    </WidgetErrorBoundary>
  );
}

export default function DynamicWidget({ modId, state, config, slotName, assetsBaseUrl, eventBus }) {
  const componentRef = useRef(widgetCache.get(modId) || null);
  const [loaded, setLoaded] = useState(widgetCache.has(modId));
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    console.log(`[DynamicWidget] mount for modId=${modId}, cached=${widgetCache.has(modId)}`);

    if (widgetCache.has(modId)) {
      console.log(`[DynamicWidget] ${modId} using cached component`);
      componentRef.current = widgetCache.get(modId);
      if (!loaded) setLoaded(true);
      return;
    }

    let cancelled = false;

    console.log(`[DynamicWidget] ${modId} fetching /widgets/${modId}/widget.jsx`);
    api.getWidget(modId)
      .then(res => {
        console.log(`[DynamicWidget] ${modId} response status=${res.status}`);
        if (!res.ok) {
          if (res.status === 404) {
            console.warn(`[DynamicWidget] ${modId} got 404 - endpoint not mounted? Server restarted?`);
            return null;
          }
          throw new Error(`HTTP ${res.status}`);
        }
        return res.text();
      })
      .then(source => {
        if (cancelled || !mountedRef.current) return;
        console.log(`[DynamicWidget] ${modId} source length=${source ? source.length : 0}`);
        if (!source) {
          if (mountedRef.current) setLoaded(true);
          return;
        }

        console.log(`[DynamicWidget] ${modId} compiling with Babel...`);
        const result = Babel.transform(source, {
          presets: [
            ['env', { modules: 'commonjs' }],
            ['react', { runtime: 'classic' }]
          ]
        });
        console.log(`[DynamicWidget] ${modId} Babel compiled, code length=${result.code.length}`);

        const factory = new Function('require', 'module', 'exports', 'React', result.code);
        const mod = { exports: {} };
        factory(requireMock, mod, mod.exports, React);

        const Comp = mod.exports.default || mod.exports;
        console.log(`[DynamicWidget] ${modId} got component type=${typeof Comp}`);
        if (typeof Comp !== 'function') {
          throw new Error('Widget must export a React component as default');
        }

        widgetCache.set(modId, Comp);
        componentRef.current = Comp;
        console.log(`[DynamicWidget] ${modId} cached and mounted successfully`);
        if (mountedRef.current) setLoaded(true);
      })
      .catch(err => {
        console.error(`[DynamicWidget] ${modId} error:`, err);
        if (!cancelled && mountedRef.current) {
          setLoaded(true);
          throw err;
        }
      });

    return () => { cancelled = true; mountedRef.current = false; };
  }, [modId, loaded]);

  if (!loaded) return <SkeletonLoader />;

  return (
    <WidgetRenderer
      componentRef={componentRef}
      modId={modId}
      state={state}
      config={config}
      slotName={slotName}
      assetsBaseUrl={assetsBaseUrl}
      eventBus={eventBus}
    />
  );
}
