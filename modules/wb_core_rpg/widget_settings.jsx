import React, { useState } from 'react';

const DEFAULT_TIERS = [
  { min: 1, max: 4, label: "Severely Impaired" },
  { min: 5, max: 8, label: "Below Average" },
  { min: 9, max: 12, label: "Average Human" },
  { min: 13, max: 16, label: "Above Average / Trained" },
  { min: 17, max: 20, label: "Expert / Peak Human" },
  { min: 21, max: 25, label: "Superhuman" },
  { min: 26, max: 30, label: "Legendary / Demigod" },
];

export default function RpgSettingsWidget({ config, onSaveConfig }) {
  const tiers = (config?.stat_tiers && config.stat_tiers.length > 0) ? config.stat_tiers : DEFAULT_TIERS;
  const [items, setItems] = useState(structuredClone(tiers));
  const [saved, setSaved] = useState(false);

  const updateItem = (index, field, value) => {
    setItems(prev => {
      const next = [...prev];
      next[index] = { ...next[index], [field]: value === undefined ? 0 : value };
      return next;
    });
    setSaved(false);
  };

  const addTier = () => {
    const last = items[items.length - 1];
    setItems(prev => [...prev, {
      min: (last?.max || 0) + 1,
      max: (last?.max || 0) + 4,
      label: "New Tier",
    }]);
    setSaved(false);
  };

  const removeTier = (index) => {
    setItems(prev => prev.filter((_, i) => i !== index));
    setSaved(false);
  };

  const saveToConfig = () => {
    onSaveConfig({ stat_tiers: items });
    setSaved(true);
  };

  const resetDefaults = () => {
    setItems(structuredClone(DEFAULT_TIERS));
    setSaved(false);
  };

  return (
    <div className="p-6 space-y-4">
      <div>
        <h3 className="text-md font-semibold text-gray-200 mb-1">Stat Tier Reference</h3>
        <p className="text-xs text-gray-500 mb-3">
          These ranges tell the AI what each stat value means narratively.
          Ranges should be contiguous and non-overlapping.
        </p>
      </div>

      <div className="space-y-1">
        <div className="grid grid-cols-[1fr_1fr_2fr_40px] gap-1 text-xs text-gray-500 px-1">
          <span>Min</span><span>Max</span><span>Label</span><span></span>
        </div>
        {items.map((item, i) => (
          <div key={i} className="grid grid-cols-[1fr_1fr_2fr_40px] gap-1">
            <input
              type="number"
              value={item.min}
              onChange={e => updateItem(i, 'min', parseInt(e.target.value) || item.min)}
              className="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-gray-200 text-xs w-full"
              min={1}
              max={100}
            />
            <input
              type="number"
              value={item.max}
              onChange={e => updateItem(i, 'max', parseInt(e.target.value) || item.max)}
              className="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-gray-200 text-xs w-full"
              min={1}
              max={100}
            />
            <input
              type="text"
              value={item.label}
              onChange={e => updateItem(i, 'label', e.target.value)}
              className="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-gray-200 text-xs w-full"
              placeholder="Label"
            />
            <button
              onClick={() => removeTier(i)}
              className="text-red-400 hover:text-red-300 text-xs"
              title="Remove tier"
            >
              ✕
            </button>
          </div>
        ))}
        <button
          onClick={addTier}
          className="text-xs text-green-400 hover:text-green-300 hover:underline mt-1"
        >
          + Add Tier
        </button>
      </div>

      <div className="flex gap-2 pt-2 border-t border-gray-700">
        <button
          onClick={saveToConfig}
          className="px-3 py-1.5 text-sm bg-purple-600 hover:bg-purple-500 text-white rounded transition-colors"
        >
          {saved ? 'Saved' : 'Save'}
        </button>
        <button
          onClick={resetDefaults}
          className="px-3 py-1.5 text-sm bg-gray-700 hover:bg-gray-600 text-gray-200 rounded transition-colors"
        >
          Reset Defaults
        </button>
        <span className="text-xs text-gray-500 self-center">
          {saved && 'Saved to module config.'}
        </span>
      </div>
    </div>
  );
}
