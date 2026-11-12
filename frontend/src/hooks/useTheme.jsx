import { createContext, useContext, useState, useCallback, useEffect, useRef } from 'react';
import { api } from '../lib/api';
import { storage } from '../lib/storage';
import { COLOR_FIELDS, DEFAULT_COLORS, getPreset, matchPreset } from '../lib/themePresets';

const ThemeContext = createContext(null);

// Apply the four base colors as inline CSS variables on <html>. These cascade
// into the color-mix derivations in index.css, re-theming the whole app. Inline
// vars win over the stylesheet :root defaults.
function applyThemeVars(colors) {
  const root = document.documentElement;
  root.style.setProperty('--app-primary', colors.primary);
  root.style.setProperty('--app-bg', colors.background);
  root.style.setProperty('--app-text', colors.text);
  root.style.setProperty('--app-dialogue', colors.dialogue);
}

export function ThemeProvider({ children }) {
  const [colors, setColors] = useState(DEFAULT_COLORS);
  const [activePreset, setActivePreset] = useState('default');
  // Message density is a local device preference (reading comfort varies by
  // screen), so it lives in localStorage rather than the server-side theme.
  const [density, setDensityState] = useState(() => {
    try {
      return storage.getItem('wb_chat_density') === 'compact' ? 'compact' : 'comfortable';
    } catch {
      return 'comfortable';
    }
  });
  // Story display mode ('feed' = classic scrolling log, 'stage' = centered
  // automatic sentence-by-sentence reader) and reading speed in chars/sec.
  const [displayMode, setDisplayModeState] = useState(() => {
    try {
      return storage.getItem('wb_display_mode') === 'stage' ? 'stage' : 'feed';
    } catch {
      return 'feed';
    }
  });
  const [readerSpeed, setReaderSpeedState] = useState(() => {
    try {
      const raw = Number(storage.getItem('wb_reader_speed'));
      return Number.isFinite(raw) && raw >= 15 && raw <= 95 ? raw : 38;
    } catch {
      return 38;
    }
  });
  const saveTimer = useRef(null);

  const setDensity = useCallback((value) => {
    const next = value === 'compact' ? 'compact' : 'comfortable';
    setDensityState(next);
    try { storage.setItem('wb_chat_density', next); } catch { /* private mode */ }
  }, []);

  const setDisplayMode = useCallback((value) => {
    const next = value === 'stage' ? 'stage' : 'feed';
    setDisplayModeState(next);
    try { storage.setItem('wb_display_mode', next); } catch { /* private mode */ }
  }, []);

  const setReaderSpeed = useCallback((value) => {
    const num = Math.round(Math.min(95, Math.max(15, Number(value) || 38)));
    setReaderSpeedState(num);
    try { storage.setItem('wb_reader_speed', String(num)); } catch { /* private mode */ }
  }, []);

  // Broadcast current reader mode generically so optional modules can stay aware,
  // and accept generic `wb:set-display-mode` requests.
  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    window.__wbReaderMode = displayMode;
    window.__wbReaderSpeed = readerSpeed;
    window.dispatchEvent(
      new CustomEvent('wb:reader-mode', { detail: { mode: displayMode, speed: readerSpeed } })
    );
    return undefined;
  }, [displayMode, readerSpeed]);

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const onSetMode = (e) => {
      const d = e?.detail || {};
      if (d.mode === 'stage' || d.mode === 'feed') setDisplayMode(d.mode);
      if (d.speed != null) setReaderSpeed(d.speed);
    };
    window.addEventListener('wb:set-display-mode', onSetMode);
    return () => window.removeEventListener('wb:set-display-mode', onSetMode);
  }, [setDisplayMode, setReaderSpeed]);

  // Persist to the backend, debounced so dragging a color picker doesn't spam.
  const persist = useCallback((preset, nextColors) => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      api.updateTheme({ preset, colors: nextColors }).catch(() => {});
    }, 400);
  }, []);

  const commit = useCallback((preset, nextColors, save = true) => {
    setColors(nextColors);
    setActivePreset(preset);
    applyThemeVars(nextColors);
    if (save) persist(preset, nextColors);
  }, [persist]);

  // Load the saved theme on mount and apply it.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.getTheme();
        const t = data.theme || {};
        const loaded = { ...DEFAULT_COLORS, ...(t.colors || {}) };
        if (cancelled) return;
        commit(t.preset || matchPreset(loaded), loaded, false);
      } catch (_) {
        if (!cancelled) applyThemeVars(DEFAULT_COLORS);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setColor = useCallback((key, value) => {
    const next = { ...colors, [key]: value };
    commit(matchPreset(next), next);
  }, [colors, commit]);

  const applyPreset = useCallback((id) => {
    const preset = getPreset(id);
    if (!preset) return;
    commit(id, { ...preset.colors });
  }, [commit]);

  const resetToDefault = useCallback(() => applyPreset('default'), [applyPreset]);

  return (
    <ThemeContext.Provider value={{ colors, activePreset, fields: COLOR_FIELDS, setColor, applyPreset, resetToDefault, density, setDensity, displayMode, setDisplayMode, readerSpeed, setReaderSpeed }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used inside ThemeProvider');
  return ctx;
}
