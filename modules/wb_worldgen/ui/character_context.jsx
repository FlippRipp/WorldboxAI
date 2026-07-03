import { useState, useEffect } from 'react';
import { api } from 'api';

// Character-generation context contributed by wb_worldgen: a world picker. The
// host owns the merged generation context; this widget reports its fragment via
// `onContext({ world_id })`. `value` is the current context (so it stays in sync
// when editing an existing character). Renders nothing if no worlds exist.
export default function CharacterContext({ value = {}, onContext }) {
  const [worlds, setWorlds] = useState([]);
  const worldId = value.world_id || '';

  useEffect(() => {
    api.listWorlds().then((d) => setWorlds(d.worlds || [])).catch(() => {});
  }, []);

  if (worlds.length === 0) return null;

  return (
    <div className="bg-gray-800/60 border border-gray-700 rounded-xl p-6 space-y-4">
      <div>
        <h3 className="text-lg font-semibold text-gray-200 mb-1">
          World Context <span className="text-gray-500 text-sm font-normal">(optional)</span>
        </h3>
        <p className="text-xs text-gray-500 mb-3">Select a world to help the AI generate theme-appropriate details.</p>
        <select
          value={worldId}
          onChange={(e) => onContext({ world_id: e.target.value || null })}
          className="w-full bg-gray-900 border border-gray-700 rounded-lg px-4 py-2.5 text-gray-200 focus:border-purple-500 focus:outline-none"
        >
          <option value="">None (generic character)</option>
          {worlds.map((w) => (
            <option key={w.id} value={w.id}>{w.name}</option>
          ))}
        </select>
      </div>
    </div>
  );
}
