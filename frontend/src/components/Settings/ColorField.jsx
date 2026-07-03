import { useEffect, useState } from 'react';

const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

// A color swatch (native picker) paired with an editable hex input. Commits only
// valid hex values upward; lets the user type freely in between.
export default function ColorField({ label, hint, value, onChange }) {
  const [text, setText] = useState(value);

  // Keep the text box in sync when the value changes externally (e.g. preset).
  useEffect(() => { setText(value); }, [value]);

  const commitText = (raw) => {
    let v = raw.trim();
    if (v && !v.startsWith('#')) v = `#${v}`;
    setText(v);
    if (HEX_RE.test(v)) onChange(v);
  };

  const valid = HEX_RE.test(text);

  return (
    <div className="flex items-center gap-3">
      <label
        className="relative w-10 h-10 rounded-lg border border-gray-700 overflow-hidden shrink-0 cursor-pointer"
        style={{ backgroundColor: valid ? text : value }}
        title="Pick a color"
      >
        <input
          type="color"
          value={valid ? text : value}
          onChange={(e) => { setText(e.target.value); onChange(e.target.value); }}
          className="absolute inset-0 opacity-0 cursor-pointer"
        />
      </label>
      <div className="flex-1 min-w-0">
        <div className="text-sm text-gray-300">{label}</div>
        {hint && <div className="text-xs text-gray-500">{hint}</div>}
      </div>
      <input
        type="text"
        value={text}
        onChange={(e) => commitText(e.target.value)}
        spellCheck={false}
        className={`w-28 bg-gray-900 border rounded px-2 py-1.5 text-sm font-mono text-gray-200 focus:outline-none ${
          valid ? 'border-gray-700 focus:border-purple-500' : 'border-red-700'
        }`}
      />
    </div>
  );
}
