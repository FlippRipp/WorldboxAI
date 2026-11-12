import { useTheme } from '../../hooks/useTheme';
import { THEME_PRESETS } from '../../lib/themePresets';
import ColorField from './ColorField';

export default function AppearanceSettings() {
  const { colors, activePreset, fields, setColor, applyPreset, resetToDefault, density, setDensity, displayMode, setDisplayMode, readerSpeed, setReaderSpeed } = useTheme();
  const approxWpm = Math.round((readerSpeed * 60) / 5.2);

  return (
    <div className="space-y-6">
      {/* Story display mode */}
      <div className="p-4 bg-gray-800/60 rounded-lg space-y-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-300 mb-1">Story display mode</h3>
          <p className="text-xs text-gray-500">Choose between the classic scrolling transcript or a centered, paced reading stage. You can also switch anytime from the story header.</p>
        </div>
        <div className="flex gap-3 flex-wrap">
          {[
            { id: 'feed', label: 'Story Log', hint: 'Classic scrolling transcript as tokens stream in' },
            { id: 'stage', label: 'Paced Stage', hint: 'Centered sentence-by-sentence reveal with History overlay' },
          ].map((opt) => (
            <button
              key={opt.id}
              onClick={() => setDisplayMode(opt.id)}
              className={`px-3 py-2 rounded-lg border text-sm text-left transition-colors ${
                displayMode === opt.id
                  ? 'border-purple-500 bg-purple-600/20 text-gray-100'
                  : 'border-gray-700 bg-gray-900/40 text-gray-300 hover:border-gray-600'
              }`}
            >
              <span className="font-medium">{opt.label}</span>
              <p className="text-xs text-gray-500 mt-0.5">{opt.hint}</p>
            </button>
          ))}
        </div>
        {displayMode === 'stage' && (
          <div className="pt-2 border-t border-gray-700/60">
            <div className="flex items-center justify-between text-xs text-gray-400 mb-1.5">
              <span>Stage reading speed</span>
              <span className="font-mono text-purple-300">~{approxWpm} WPM</span>
            </div>
            <input
              type="range"
              min="15"
              max="95"
              step="1"
              value={readerSpeed}
              onChange={(e) => setReaderSpeed(Number(e.target.value))}
              className="w-full accent-purple-500"
            />
          </div>
        )}
      </div>

      {/* Message density */}
      <div className="p-4 bg-gray-800/60 rounded-lg">
        <h3 className="text-sm font-semibold text-gray-300 mb-1">Message density</h3>
        <p className="text-xs text-gray-500 mb-3">How much space story messages take up. Saved on this device.</p>
        <div className="flex gap-3">
          {[
            { id: 'comfortable', label: 'Comfortable', hint: 'Larger text, roomy spacing' },
            { id: 'compact', label: 'Compact', hint: 'Smaller text, tighter spacing' },
          ].map((opt) => (
            <button
              key={opt.id}
              onClick={() => setDensity(opt.id)}
              className={`px-3 py-2 rounded-lg border text-sm text-left transition-colors ${
                density === opt.id
                  ? 'border-purple-500 bg-purple-600/20 text-gray-100'
                  : 'border-gray-700 bg-gray-900/40 text-gray-300 hover:border-gray-600'
              }`}
            >
              <span className="font-medium">{opt.label}</span>
              <p className="text-xs text-gray-500 mt-0.5">{opt.hint}</p>
            </button>
          ))}
        </div>
      </div>

      {/* Presets */}
      <div className="p-4 bg-gray-800/60 rounded-lg">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Presets</h3>
        <div className="flex gap-3 flex-wrap">
          {THEME_PRESETS.map((p) => {
            const selected = activePreset === p.id;
            return (
              <button
                key={p.id}
                onClick={() => applyPreset(p.id)}
                className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-sm transition-colors ${
                  selected
                    ? 'border-purple-500 bg-purple-600/20 text-gray-100'
                    : 'border-gray-700 bg-gray-900/40 text-gray-300 hover:border-gray-600'
                }`}
              >
                <span className="flex -space-x-1">
                  {['background', 'primary', 'text', 'dialogue'].map((k) => (
                    <span
                      key={k}
                      className="w-3.5 h-3.5 rounded-full border border-black/40"
                      style={{ backgroundColor: p.colors[k] }}
                    />
                  ))}
                </span>
                {p.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Custom pickers */}
      <div className="p-4 bg-gray-800/60 rounded-lg space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-gray-300">
            Custom colors{activePreset === 'custom' && <span className="ml-2 text-xs text-purple-400">(custom)</span>}
          </h3>
          <button
            onClick={resetToDefault}
            className="text-xs px-2.5 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors"
          >
            Reset to default
          </button>
        </div>
        {fields.map((f) => (
          <ColorField
            key={f.key}
            label={f.label}
            hint={f.hint}
            value={colors[f.key]}
            onChange={(v) => setColor(f.key, v)}
          />
        ))}
      </div>

      {/* Live preview — uses ordinary themed classes, so it reflects changes instantly */}
      <div className="p-4 bg-gray-800/60 rounded-lg">
        <h3 className="text-sm font-semibold text-gray-300 mb-3">Preview</h3>
        <div className="bg-gray-900 border border-gray-700 rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-gray-100">Sample panel</span>
            <button className="px-3 py-1.5 bg-purple-600 hover:bg-purple-500 rounded text-sm text-white transition-colors">
              Primary button
            </button>
          </div>
          <p className="text-gray-200 text-sm leading-relaxed">
            The tavern keeper looked up as you entered.{' '}
            <span className="text-quote">"We don't see many travelers this time of year,"</span>{' '}
            she said, wiping down the counter.
          </p>
          <p className="text-gray-400 text-xs">Muted secondary text looks like this.</p>
        </div>
      </div>
    </div>
  );
}
