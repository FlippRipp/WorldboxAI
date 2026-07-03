import { useSyncExternalStore } from 'react';

// Reactive matchMedia. Use '(max-width: 1023px)' to match Tailwind's lg
// breakpoint so JS behavior gates agree with lg: layout classes.
export function useMediaQuery(query) {
  return useSyncExternalStore(
    (notify) => {
      const mql = window.matchMedia(query);
      mql.addEventListener('change', notify);
      return () => mql.removeEventListener('change', notify);
    },
    () => window.matchMedia(query).matches,
  );
}
