import { useEffect, useRef } from 'react';

// Ordered LIFO registry of transient back-handlers (modals, drawers, module
// subviews). Module UIs loaded at runtime can also register via
// `window.__wbRegisterBackHandler(fn)` without needing a host import.
const handlers = [];

export function registerBackHandler(fn) {
  handlers.push(fn);
  syncGuard();
  return () => {
    const idx = handlers.lastIndexOf(fn);
    if (idx !== -1) handlers.splice(idx, 1);
    syncGuard();
  };
}

if (typeof window !== 'undefined') {
  window.__wbRegisterBackHandler = registerBackHandler;
}

let rootCanGoBack = false;
let rootOnBack = null;
let guardArmed = false;
let suppressNextPop = false;

function hasAnyBackTarget() {
  return handlers.length > 0 || rootCanGoBack;
}

function syncGuard() {
  if (typeof window === 'undefined') return;
  const shouldArm = hasAnyBackTarget();
  if (shouldArm && !guardArmed) {
    window.history.pushState({ wbBackGuard: true }, '');
    guardArmed = true;
  } else if (!shouldArm && guardArmed) {
    guardArmed = false;
    suppressNextPop = true;
    window.history.back();
  }
}

/**
 * Register a modal, drawer, or sub-screen so Android Back / browser Back
 * dismisses it before stepping back the parent screen.
 */
export function useBackTrap(active, onDismiss) {
  const dismissRef = useRef(onDismiss);
  dismissRef.current = onDismiss;

  useEffect(() => {
    if (!active) return undefined;
    return registerBackHandler(() => dismissRef.current?.());
  }, [active]);
}

/**
 * Root navigation back controller (mounted once in AppContent).
 * Uses a single history-sentinel entry so opening/closing drawers and modals
 * in the same click never causes async `history.back()` race conditions.
 */
export function useRootBackNavigation(canGoBack, onStepBack) {
  const stepBackRef = useRef(onStepBack);
  stepBackRef.current = onStepBack;

  useEffect(() => {
    rootCanGoBack = Boolean(canGoBack);
    rootOnBack = () => stepBackRef.current?.();
    // Defer disarming by a microtask so drawer->modal handoffs in the same
    // event loop tick keep the sentinel armed continuously.
    const t = setTimeout(syncGuard, 0);
    return () => clearTimeout(t);
  }, [canGoBack]);

  useEffect(() => {
    const onPopState = () => {
      if (suppressNextPop) {
        suppressNextPop = false;
        return;
      }
      // Browser just consumed our sentinel entry.
      guardArmed = false;

      if (handlers.length > 0) {
        const top = handlers[handlers.length - 1];
        top?.();
      } else if (rootCanGoBack && rootOnBack) {
        rootOnBack();
      }

      // Re-arm on the next tick if we still aren't at the root menu.
      setTimeout(syncGuard, 0);
    };

    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);
}
