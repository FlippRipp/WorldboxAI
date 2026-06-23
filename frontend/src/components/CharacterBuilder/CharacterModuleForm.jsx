import React, { useState, useEffect, useRef } from 'react';
import * as Babel from '@babel/standalone';
import WidgetErrorBoundary from '../shared/WidgetErrorBoundary';
import SkeletonLoader from '../shared/SkeletonLoader';
import { api } from '../../lib/api';

const widgetCache = new Map();

function requireMock(moduleName) {
  if (moduleName === 'react') return React;
  throw new Error(`Module "${moduleName}" not available to widgets`);
}

function WidgetRenderer({ componentRef, value, onChange, worldId, modId }) {
  const Comp = componentRef.current;
  if (!Comp) return null;
  return (
    <WidgetErrorBoundary modId={modId}>
      <Comp value={value} onChange={onChange} worldId={worldId} />
    </WidgetErrorBoundary>
  );
}

export default function CharacterModuleForm({ modId, value, onChange, worldId }) {
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

    api.getWidgetFile(modId, 'character_widget.jsx')
      .then(res => {
        if (!res.ok) {
          if (res.status === 404) return null;
          throw new Error(`HTTP ${res.status}`);
        }
        return res.text();
      })
      .then(source => {
        if (cancelled || !mountedRef.current) return;
        if (!source) {
          if (mountedRef.current) setLoaded(true);
          return;
        }

        const result = Babel.transform(source, {
          presets: [
            ['env', { modules: 'commonjs' }],
            ['react', { runtime: 'classic' }]
          ]
        });

        const factory = new Function('require', 'module', 'exports', 'React', result.code);
        const mod = { exports: {} };
        factory(requireMock, mod, mod.exports, React);

        const Comp = mod.exports.default || mod.exports;
        if (typeof Comp !== 'function') {
          throw new Error('Widget must export a React component as default');
        }

        widgetCache.set(cacheKey, Comp);
        componentRef.current = Comp;
        if (mountedRef.current) setLoaded(true);
      })
      .catch(err => {
        console.error(`Error loading character widget for ${modId}:`, err);
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
      worldId={worldId}
      modId={modId}
    />
  );
}
