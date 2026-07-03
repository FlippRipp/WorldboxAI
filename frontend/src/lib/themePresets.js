// The four semantic colors that drive the whole UI theme. Keep keys in sync
// with the backend DEFAULT_COLORS (backend/engine/theme_store.py) and the
// --app-* variables in index.css.
export const COLOR_FIELDS = [
  { key: 'primary',    label: 'Primary / accent', hint: 'Buttons, highlights, headings' },
  { key: 'background', label: 'Background',        hint: 'App background, panels, dialogs' },
  { key: 'text',       label: 'Text',             hint: 'General body text' },
  { key: 'dialogue',   label: 'In-story dialogue', hint: 'Quoted character speech' },
];

export const DEFAULT_COLORS = {
  primary: '#9333ea',
  background: '#111827',
  text: '#e5e7eb',
  dialogue: '#d4a574',
};

// Each preset is just the four base colors. Selecting a preset fills the pickers.
export const THEME_PRESETS = [
  {
    id: 'default',
    label: 'WorldBox (default)',
    colors: { ...DEFAULT_COLORS },
  },
  {
    // SillyTavern's built-in "Dark Lite" theme, exact values.
    id: 'sillytavern',
    label: 'SillyTavern',
    colors: {
      primary: '#e18a24',     // ST signature orange
      background: '#171717',  // blur_tint
      text: '#dcdcd2',        // main_text
      dialogue: '#e18a24',    // quote_text
    },
  },
  {
    id: 'slate',
    label: 'Slate',
    colors: {
      primary: '#6366f1',
      background: '#0f172a',
      text: '#e2e8f0',
      dialogue: '#f59e0b',
    },
  },
  {
    id: 'high-contrast',
    label: 'High contrast',
    colors: {
      primary: '#22d3ee',
      background: '#000000',
      text: '#ffffff',
      dialogue: '#ffd400',
    },
  },
];

export function getPreset(id) {
  return THEME_PRESETS.find((p) => p.id === id) || null;
}

// Compare two color sets to detect when custom edits match a known preset.
export function matchPreset(colors) {
  for (const p of THEME_PRESETS) {
    if (COLOR_FIELDS.every(({ key }) => p.colors[key] === colors[key])) return p.id;
  }
  return 'custom';
}
