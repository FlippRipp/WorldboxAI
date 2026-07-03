import { useRef, useState, useCallback, useLayoutEffect, useEffect } from 'react';

// Keep a scroll container pinned to the bottom as its content grows (e.g. while
// tokens stream in), but only while the user is already at the bottom. If the
// user scrolls up, auto-scroll pauses until they scroll back down. Returns a
// ref for the scrollable element, an onScroll handler to attach to it, a
// pin() to force re-stick (e.g. when a fresh stream starts), a reactive
// `pinned` flag (for a scroll-to-bottom affordance), and scrollToBottom()
// for smooth user-triggered returns.
//
// While pinned, small content growth (streamed lines) is followed with an
// animated glide rather than an instant snap, so the text scrolls smoothly
// under the reader's eyes. Large gaps (initial load, re-pin on a fresh turn)
// still snap instantly so we never animate through pages of history.
//
// `deps` is the array of values whose changes should trigger a stick check
// (must have a stable length across renders, like any hook deps array).
//
// `onUserScroll(delta, el)` fires only for scrolls the hook did not cause
// itself (finger drags, wheel) — programmatic glide/snap writes are filtered
// out via lastWriteRef, so e.g. a scroll-direction header can't react to the
// streaming auto-scroll.
export function useStickToBottom(deps, { enabled = true, threshold = 48, onUserScroll } = {}) {
  const ref = useRef(null);
  const pinnedRef = useRef(true);
  const [pinned, setPinned] = useState(true);

  // rAF id of the follow animation, and the last scrollTop we wrote
  // programmatically (to tell our own scroll events apart from the user's).
  const rafRef = useRef(0);
  const lastWriteRef = useRef(-1);
  const lastFrameTimeRef = useRef(0);
  const prevScrollTopRef = useRef(0);

  // Through a ref so onScroll keeps a stable identity across renders.
  const onUserScrollRef = useRef(onUserScroll);
  onUserScrollRef.current = onUserScroll;

  const isAtBottom = useCallback(() => {
    const el = ref.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
  }, [threshold]);

  const stopFollow = useCallback(() => {
    if (rafRef.current) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
    }
  }, []);

  // Glide toward the bottom with time-based exponential smoothing. The target
  // is recomputed every frame, so the glide tracks content that keeps growing
  // mid-animation. Converges (minimum step) and stops itself at the bottom.
  const startFollow = useCallback(() => {
    if (rafRef.current) return;
    const TAU = 120; // ms; smaller = snappier, larger = floatier
    lastFrameTimeRef.current = performance.now();
    const step = (now) => {
      rafRef.current = 0;
      const el = ref.current;
      if (!el || !pinnedRef.current) return;
      const dt = Math.min(now - lastFrameTimeRef.current, 100);
      lastFrameTimeRef.current = now;
      const target = el.scrollHeight - el.clientHeight;
      const dist = target - el.scrollTop;
      if (dist <= 0.5) {
        el.scrollTop = target;
        lastWriteRef.current = el.scrollTop;
        return;
      }
      const move = Math.max(dist * (1 - Math.exp(-dt / TAU)), 0.5);
      el.scrollTop = Math.min(el.scrollTop + move, target);
      lastWriteRef.current = el.scrollTop;
      rafRef.current = requestAnimationFrame(step);
    };
    rafRef.current = requestAnimationFrame(step);
  }, []);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const isProgrammatic = Math.abs(el.scrollTop - lastWriteRef.current) < 1;
    const delta = el.scrollTop - prevScrollTopRef.current;
    prevScrollTopRef.current = el.scrollTop;
    if (!isProgrammatic && onUserScrollRef.current) onUserScrollRef.current(delta, el);
    const p = isAtBottom();
    // Mid-glide the viewport can lag more than `threshold` behind the bottom;
    // ignore scroll events we caused ourselves so the catch-up animation
    // doesn't unpin the reader. User-driven scrolls land elsewhere.
    if (!p && Math.abs(el.scrollTop - lastWriteRef.current) < 1) return;
    pinnedRef.current = p;
    setPinned(p);
  }, [isAtBottom]);

  const pin = useCallback(() => {
    pinnedRef.current = true;
    setPinned(true);
  }, []);

  // Use our own glide (not el.scrollTo smooth) so every frame is recorded in
  // lastWriteRef and the ride down never counts as a user scroll.
  const scrollToBottom = useCallback(() => {
    pin();
    startFollow();
  }, [pin, startFollow]);

  // Run synchronously after DOM mutations: snap instantly across big gaps,
  // otherwise let the follow animation glide over the newly added lines.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || !enabled) {
      stopFollow();
      return;
    }
    if (!pinnedRef.current) return;
    const target = el.scrollHeight - el.clientHeight;
    const dist = target - el.scrollTop;
    if (dist > el.clientHeight) {
      stopFollow();
      el.scrollTop = target;
      lastWriteRef.current = el.scrollTop;
    } else if (dist > 0) {
      startFollow();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);

  useEffect(() => stopFollow, [stopFollow]);

  return { ref, onScroll, pin, pinned, scrollToBottom };
}
