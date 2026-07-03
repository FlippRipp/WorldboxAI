// Runtime loader for module-supplied JSX (widgets and full-screen views).
//
// Modules ship plain .jsx served from /widgets/{modId}/<file>. This loader
// fetches, Babel-transforms, and executes that code in the browser. Unlike a
// bare `new Function` eval, it supports:
//   - a curated set of shared dependencies (react, react-dom, the api client)
//     exposed to module code via require/import, and
//   - multi-file modules: a module file may import sibling files (e.g.
//     `import StepCard from './StepCard'`), which are resolved, fetched, and
//     compiled recursively.
//
// To grow what modules can import, add to BUILTINS below.

import React from 'react';
import ReactDOM from 'react-dom';
import * as Babel from '@babel/standalone';
import * as d3Delaunay from 'd3-delaunay';
import { api } from '../../lib/api';

const BUILTINS = {
  react: React,
  'react-dom': ReactDOM,
  api: { api },
  // Exposed for module map UIs (e.g. wb_worldgen MapRenderer's Voronoi).
  'd3-delaunay': d3Delaunay,
};

// modId -> entryFile -> Promise<Component>
const componentCache = new Map();

function cacheKey(modId, entryFile) {
  return `${modId}::${entryFile}`;
}

// Resolve a relative specifier (./x, ../y/z) against the importing file's
// directory, returning a normalized module path (no extension).
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

// Pull import/require specifiers out of raw source (before transform).
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

function transform(source) {
  return Babel.transform(source, {
    presets: [
      ['env', { modules: 'commonjs' }],
      ['react', { runtime: 'classic' }],
    ],
  }).code;
}

// Recursively fetch + compile a module file and all of its sibling imports,
// populating `registry` (normalized path -> { code, deps }).
async function collectModule(modId, modulePath, registry) {
  const normalized = modulePath.replace(/\.jsx?$/, '');
  if (registry.has(normalized)) return;
  registry.set(normalized, null); // mark visiting to break cycles

  const source = await fetchSource(modId, normalized);
  const relativeDeps = {};
  for (const spec of findSpecifiers(source)) {
    if (spec.startsWith('.')) {
      relativeDeps[spec] = resolvePath(normalized, spec);
    }
  }
  registry.set(normalized, { code: transform(source), deps: relativeDeps });

  for (const depPath of Object.values(relativeDeps)) {
    await collectModule(modId, depPath, registry);
  }
}

// Build a require() that resolves builtins by name and sibling files from the
// compiled registry, executing each file lazily and caching its exports.
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

// Load a module's component (default export) from `entryFile`. Cached per
// (modId, entryFile). Returns a React component function.
export function loadModuleComponent(modId, entryFile = 'widget.jsx') {
  const key = cacheKey(modId, entryFile);
  if (componentCache.has(key)) return componentCache.get(key);

  const promise = (async () => {
    const registry = new Map();
    const entryPath = entryFile.replace(/\.jsx?$/, '');
    await collectModule(modId, entryPath, registry);
    const evaluate = makeRuntime(registry);
    const exports = evaluate(entryPath);
    const Comp = exports.default || exports;
    if (typeof Comp !== 'function') {
      throw new Error(`Module ${modId}/${entryFile} must export a React component as default`);
    }
    return Comp;
  })();

  componentCache.set(key, promise);
  // On failure, drop the cache entry so a later mount can retry.
  promise.catch(() => componentCache.delete(key));
  return promise;
}
