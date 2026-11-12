// Runtime loader for module-supplied JSX (widgets and full-screen views).
//
// Fast path: `/api/widgets/{modId}/bundle?entry=<file>` returns the entry file
// and all of its relative imports pre-compiled to CommonJS in a single
// ETag-validated HTTP response, so mobile clients never download or execute the
// ~2.8 MB `@babel/standalone` bundle. If a response ever lacks pre-compiled
// `code` (e.g. Node unavailable on the host) or the bundle endpoint isn't
// present, `@babel/standalone` is dynamically imported on demand as a fallback.

import React from 'react';
import ReactDOM from 'react-dom';
import * as d3Delaunay from 'd3-delaunay';
import { api } from '../../lib/api';
import { storage } from '../../lib/storage';

const BUILTINS = {
  react: React,
  'react-dom': ReactDOM,
  api: { api },
  // Profile-namespaced localStorage; module code must use this instead of
  // window.localStorage so demo mode / profiles don't share browser state.
  storage: { storage },
  // Exposed for module map UIs (e.g. wb_worldgen MapRenderer's Voronoi).
  'd3-delaunay': d3Delaunay,
};

// modId::entryFile -> Promise<Component>
const componentCache = new Map();

let babelPromise = null;
function loadBabel() {
  if (!babelPromise) {
    babelPromise = import('@babel/standalone');
  }
  return babelPromise;
}

async function transformFallback(source) {
  const Babel = await loadBabel();
  return Babel.transform(source, {
    presets: [
      ['env', { modules: 'commonjs' }],
      ['react', { runtime: 'classic' }],
    ],
  }).code;
}

function cacheKey(modId, entryFile) {
  return `${modId}::${entryFile}`;
}

function resolvePath(fromPath, spec) {
  const fromDir = fromPath.includes('/') ? fromPath.slice(0, fromPath.lastIndexOf('/')) : '';
  const segments = (fromDir ? fromDir.split('/') : []).filter(Boolean);
  for (const part of spec.split('/')) {
    if (part === '' || part === '.') continue;
    if (part === '..') segments.pop();
    else segments.push(part);
  }
  return segments.join('/').replace(/\.jsx?$/, '');
}

function findSpecifiers(source) {
  const specs = new Set();
  const re = /(?:import[^'"]*?from\s*|import\s*|require\s*\(\s*|export[^'"]*?from\s*)['"]([^'"]+)['"]/g;
  let m;
  while ((m = re.exec(source)) !== null) specs.add(m[1]);
  return [...specs];
}

async function fetchSource(modId, modulePath) {
  const filename = modulePath.endsWith('.jsx') ? modulePath : `${modulePath}.jsx`;
  const res = await api.getWidgetFile(modId, filename);
  if (!res.ok) throw new Error(`Failed to fetch ${modId}/${filename} (HTTP ${res.status})`);
  return res.text();
}

// Fallback recursive collector (parallelized with Promise.all).
async function collectModuleFallback(modId, modulePath, registry) {
  const normalized = modulePath.replace(/\.jsx?$/, '');
  if (registry.has(normalized)) return;
  registry.set(normalized, null);

  const source = await fetchSource(modId, normalized);
  const relativeDeps = {};
  for (const spec of findSpecifiers(source)) {
    if (spec.startsWith('.')) {
      relativeDeps[spec] = resolvePath(normalized, spec);
    }
  }
  const [code] = await Promise.all([
    transformFallback(source),
    ...Object.values(relativeDeps).map((depPath) => collectModuleFallback(modId, depPath, registry)),
  ]);
  registry.set(normalized, { code, deps: relativeDeps });
}

async function loadRegistry(modId, entryPath) {
  let bundleNotFound = false;
  try {
    const res = await api.getWidgetBundle(modId, `${entryPath}.jsx`);
    if (res.status === 404) {
      bundleNotFound = true;
    } else if (res.ok) {
      const payload = await res.json();
      const files = payload?.files || {};
      const registry = new Map();
      await Promise.all(
        Object.entries(files).map(async ([normPath, info]) => {
          const code = info.code ?? (await transformFallback(info.source || ''));
          registry.set(normPath, { code, deps: info.deps || {} });
        })
      );
      if (registry.has(payload.entry || entryPath)) {
        return { registry, resolvedEntry: payload.entry || entryPath };
      }
    }
  } catch {
    // Fall through to per-file loader below.
  }
  if (bundleNotFound) {
    const err = new Error(`Widget file not found: ${modId}/${entryPath}.jsx`);
    err.status = 404;
    throw err;
  }

  const registry = new Map();
  await collectModuleFallback(modId, entryPath, registry);
  return { registry, resolvedEntry: entryPath };
}

function makeRuntime(registry) {
  const evaluated = new Map();

  function evaluate(normalized) {
    if (evaluated.has(normalized)) return evaluated.get(normalized);
    const entry = registry.get(normalized);
    if (!entry) throw new Error(`Module file not found: ${normalized}`);

    const moduleObj = { exports: {} };
    evaluated.set(normalized, moduleObj.exports);

    const localRequire = (spec) => {
      if (Object.prototype.hasOwnProperty.call(BUILTINS, spec)) return BUILTINS[spec];
      if (spec.startsWith('.')) return evaluate(entry.deps[spec]);
      throw new Error(`Module "${spec}" is not available to module code`);
    };

    const factory = new Function('require', 'module', 'exports', 'React', entry.code);
    factory(localRequire, moduleObj, moduleObj.exports, React);
    evaluated.set(normalized, moduleObj.exports);
    return moduleObj.exports;
  }

  return evaluate;
}

export function loadModuleComponent(modId, entryFile = 'widget.jsx') {
  const key = cacheKey(modId, entryFile);
  if (componentCache.has(key)) return componentCache.get(key);

  const promise = (async () => {
    const entryPath = entryFile.replace(/\.jsx?$/, '');
    const { registry, resolvedEntry } = await loadRegistry(modId, entryPath);
    const evaluate = makeRuntime(registry);
    const exports = evaluate(resolvedEntry);
    const Comp = exports.default || exports;
    if (typeof Comp !== 'function') {
      throw new Error(`Module ${modId}/${entryFile} must export a React component as default`);
    }
    return Comp;
  })();

  componentCache.set(key, promise);
  promise.catch(() => componentCache.delete(key));
  return promise;
}

if (typeof window !== 'undefined') {
  window.__wbLoadModuleComponent = loadModuleComponent;
}
