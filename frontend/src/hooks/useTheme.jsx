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
  const saveTimer = useRef(null);

  const setDensity = useCallback((value) => {
    const next = value === 'compact' ? 'compact' : 'comfortable';
    setDensityState(next);
    try { storage.setItem('wb_chat_density', next); } catch { /* private mode */ }
  }, []);

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
    <ThemeContext.Provider value={{ colors, activePreset, fields: COLOR_FIELDS, setColor, applyPreset, resetToDefault, density, setDensity }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used inside ThemeProvider');
  return ctx;
}
