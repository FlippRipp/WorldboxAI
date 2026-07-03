import { useState } from 'react';

/**
 * TerrainStepView — body for the `terrain_generation` step.
 *
 * The backend generates a heightmap/biome raster per surface layer and persists
 * a rendered `biome.png` / `hillshade.png` under the world's terrain directory.
 * This view previews those images (one per layer) alongside the textual summary
 * that already lives in the step data, so the user can actually see the terrain
 * that was produced before regions/landmarks are authored on top of it.
 */
export default function TerrainStepView({ editedData, worldId }) {
  const [imageKind, setImageKind] = useState('biome');
  const layers = Array.isArray(editedData?.layers) ? editedData.layers : [];

  if (!worldId || layers.length === 0) {
    return (
      <div className="text-sm text-gray-400 py-4">
        No terrain has been generated yet.
      </div>
    );
  }

  const imageUrl = (layerId) =>
    `/api/world/${encodeURIComponent(worldId)}/terrain/${encodeURIComponent(layerId)}/${imageKind}`;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm">
        <span className="text-gray-400">View:</span>
        {['biome', 'hillshade'].map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => setImageKind(kind)}
            className={`px-2 py-1 rounded capitalize ${
              imageKind === kind
                ? 'bg-indigo-600 text-white'
                : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
            }`}
          >
            {kind}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {layers.map((layer) => {
          const layerId = layer.layer_id || 'main';
          return (
            <div
              key={layerId}
              className="rounded-lg border border-gray-700 overflow-hidden bg-gray-800"
            >
              <img
                src={imageUrl(layerId)}
                alt={`${layer.name || layerId} ${imageKind}`}
                className="w-full block bg-gray-900"
                style={{ imageRendering: 'pixelated' }}
              />
              <div className="p-3 space-y-1">
                <div className="font-medium text-gray-100">
                  {layer.name || layerId}
                </div>
                {layer.summary && (
                  <p className="text-xs text-gray-400 leading-relaxed">
                    {layer.summary}
                  </p>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
