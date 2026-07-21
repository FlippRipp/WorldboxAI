import { useState, useCallback, useEffect, useMemo } from 'react';
import { api } from 'api';
import { storage } from 'storage';

const BIOME_COLORS_KEY = 'terrainlab_biome_colors';

// Biome/climate/colour params that the fast "Regenerate Biomes" path re-applies
// on the existing eroded terrain (no heightmap/erosion/rivers re-run).
const BIOME_PARAM_KEYS = [
  'biome_mode', 'fantasy_overlay', 'biome_blend', 'equator', 'temp_band',
  'lapse', 'wind_dir', 'humidity', 'orographic', 'aridity', 'alpine_aridity',
  'rock_line', 'snow_line', 'alpine_blend',
  'river_moisture', 'forests', 'forest_density', 'relief', 'hillshade_strength',
];

const _h2 = (n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0');
const rgbToHex = ([r, g, b]) => `#${_h2(r)}${_h2(g)}${_h2(b)}`;
const hexToRgb = (hex) => {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [0, 0, 0];
};

const DEFAULTS = {
  seed: -1,
  resolution: 512,
  octaves: 6,
  warp: 0.35,
  island: 0.5,
  mountain_strength: 0.85,
  mountain_coverage: 0.5,
  mountain_sharpness: 2.5,
  spline_ridges: 0,
  spline_ridge_strength: 0.5,
  spline_ridge_width: 0.12,
  spline_ridge_length: 1.0,
  redistribution: 2.2,
  thermal_iterations: 20,
  droplets: 60000,
  erosion_strength: 1.0,
  erosion_backend: 'auto',
  hydrology_model: 'momentum',
  momentum_iterations: 60,
  momentum_particles: 8000,
  momentum_transfer: 0.8,
  discharge_alpha: 0.4,
  sea_level: 0.4,
  rivers: true,
  lakes: true,
  lake_min_area: 50,
  lake_min_depth: 0.012,
  breach: true,
  breach_max_depth: 0.05,
  deltas: true,
  delta_size: 1.0,
  river_density: 0.5,
  river_carve: 0.02,
  river_meander: 1.2,
  biome_mode: 'realistic',
  fantasy_overlay: false,
  equator: 0.5,
  temp_band: 1.0,
  lapse: 0.6,
  wind_dir: 270,
  humidity: 1.0,
  orographic: 1.0,
  aridity: 0.0,
  alpine_aridity: 0.6,
  rock_line: 0.45,
  snow_line: 0.72,
  alpine_blend: 0.23,
  river_moisture: 0.3,
  forests: true,
  forest_density: 0.5,
  biome_blend: 0.7,
  relief: 16.0,
  hillshade_strength: 1.5,
  coastal_smooth: false,
  coastal_smooth_width: 0.08,
  coastal_smooth_strength: 1.0,
  craters: false,
  crater_count: 5,
  crater_min_radius: 0.02,
  crater_max_radius: 0.08,
  crater_depth: 0.15,
  crater_rim_height: 0.05,
  crater_ejecta_falloff: 3.0,
  crater_age: 'ancient',
};

// All slider metadata, keyed by param name. Sections below reference these by key.
const SLIDER_META = {
  resolution: { label: 'Resolution', min: 128, max: 4096, step: 128,
    desc: 'Grid size of the heightmap. Higher = more detail but slower to generate. 4096 is full 4K and can take several seconds.' },
  octaves: { label: 'Noise Octaves', min: 1, max: 9, step: 1,
    desc: 'Layers of fractal noise summed for the base land. More octaves add finer detail on top of the broad shapes.' },
  warp: { label: 'Domain Warp', min: 0, max: 1, step: 0.05,
    desc: 'Bends the noise so coastlines and terrain meander organically instead of looking blobby. 0 = no warping.' },
  island: { label: 'Island Falloff', min: 0, max: 2, step: 0.1,
    desc: 'Pushes the map edges toward sea so land concentrates centrally. 0 = land can reach the borders; higher = more island-like.' },
  mountain_strength: { label: 'Mountain Height', min: 0, max: 3.0, step: 0.05,
    desc: '0–1 sets the true height of the mountain ranges (now sharp peaks, no plateaus). 1–3 keeps the same geometry but makes mountains read bigger — stronger relief, deeper shading, and lower/larger snow & rock caps.' },
  mountain_coverage: { label: 'Mountain Coverage', min: 0, max: 1.5, step: 0.05,
    desc: 'How much of the land is mountainous. Lower values give isolated chains; higher values blanket more of the map. Values past ~1.15 fully cover the map (the range-clustering belts disappear).' },
  mountain_sharpness: { label: 'Ridge Sharpness', min: 0.5, max: 4, step: 0.1,
    desc: 'Thinness of the mountain ridgelines. Higher = thin, jagged ridges; lower = broad, rounded massifs.' },
  spline_ridges: { label: 'Mountain Arcs', min: 0, max: 15, step: 1,
    desc: 'Number of explicit mountain arcs that cross the map (0 = disabled). Arcs are curved chains derived from the seed, adding height in areas that noise mountains don\'t already cover.' },
  spline_ridge_strength: { label: 'Arc Strength', min: 0.1, max: 1.0, step: 0.05,
    desc: 'How much height the mountain arcs add along their centerline. Combined with Arc Width, this controls how dramatic the forced ranges are.' },
  spline_ridge_width: { label: 'Arc Width', min: 0.05, max: 0.3, step: 0.01,
    desc: 'How wide the mountain arcs are (as a fraction of the map). Narrow values give sharp single ridges; wider values create broad mountain belts.' },
  spline_ridge_length: { label: 'Arc Length', min: 0.3, max: 10, step: 0.05,
    desc: 'How far each arc spans, as a fraction of the map diagonal. Lower values give short, local ranges; 1.0 roughly crosses the map; higher extends beyond the edges.' },
  redistribution: { label: 'Lowland Flattening', min: 0.5, max: 4, step: 0.1,
    desc: 'Power curve on elevation. >1 flattens the abundant mid heights into broad lowlands while keeping peaks high.' },
  thermal_iterations: { label: 'Thermal Iterations', min: 0, max: 80, step: 5,
    desc: 'Passes of thermal erosion: slopes steeper than the talus angle slide downhill, smoothing cliffs into scree.' },
  droplets: { label: 'Erosion Droplets', min: 0, max: 300000, step: 10000,
    desc: 'Number of water droplets simulated for hydraulic erosion. More droplets carve more dendritic valleys (slower).' },
  erosion_strength: { label: 'Erosion Strength', min: 0, max: 3, step: 0.1,
    desc: 'Intensity of droplet erosion. Higher values let each droplet carry more sediment and cut more aggressively, giving deeper, more pronounced valleys.' },
  momentum_iterations: { label: 'Flow Iterations', min: 20, max: 80, step: 5,
    desc: 'Particle batches in the momentum erosion. Each batch refines the discharge/momentum maps that carve the channels. Stable up to ~70; beyond that the simulated flow can run away and over-incise.' },
  momentum_particles: { label: 'Particles / Batch', min: 2000, max: 20000, step: 1000,
    desc: 'Water particles released per batch. More particles sample more of the map each pass, giving denser, smoother drainage networks (slower).' },
  momentum_transfer: { label: 'Meander Strength', min: 0, max: 2, step: 0.1,
    desc: 'How strongly stored stream momentum steers later particles toward outer banks. This is the centrifugal coupling that makes rivers meander physically. 0 = straight dendritic valleys; higher = more sinuous channels and oxbows.' },
  discharge_alpha: { label: 'Flow Memory', min: 0.1, max: 0.9, step: 0.05,
    desc: 'How fast each batch’s flow blends into the persistent discharge map. Lower = smoother, more stable channels with longer memory; higher = channels react faster but can flicker.' },
  sea_level: { label: 'Sea Level', min: 0, max: 1, step: 0.02,
    desc: 'Target ocean fraction — e.g. 0.4 means ~40% of the map is sea. Land coverage stays stable across seeds.' },
  river_density: { label: 'River Density', min: 0, max: 1, step: 0.05,
    desc: 'How dense the river network is. Higher values turn smaller tributaries into rivers; lower keeps only the major trunks.' },
  river_carve: { label: 'River Carving', min: 0, max: 0.1, step: 0.005,
    desc: 'How deeply rivers incise their channels into the terrain. 0 leaves the heightmap untouched; higher cuts visible valleys.' },
  river_meander: { label: 'River Meander', min: 0, max: 4, step: 0.1,
    desc: 'Strength of meandering. Bends grow as a natural instability (Menger-curvature amplification) — straight on steep ground, sweeping S-curves on flat lowlands, scaled by river size (Strahler order).' },
  lake_min_area: { label: 'Lake Min Size', min: 5, max: 400, step: 5,
    desc: 'Smallest depression (in cells) that becomes a lake. Larger values keep only big lakes; smaller values allow many ponds.' },
  lake_min_depth: { label: 'Lake Min Depth', min: 0.002, max: 0.06, step: 0.002,
    desc: 'Shallowest basin (in height units) that becomes a lake. Shallow puddles below this are drained or filled instead.' },
  breach_max_depth: { label: 'Breach Depth', min: 0, max: 0.2, step: 0.01,
    desc: 'Basins shallower than this (and not lakes) get a drainage notch carved through their rim, becoming natural valleys instead of flat-filled plains. 0 = always fill.' },
  delta_size: { label: 'Delta Size', min: 0.3, max: 2.5, step: 0.1,
    desc: 'Extent of the L-system distributary deltas that fan out where large rivers meet the sea. Larger values build bigger bird-foot deltas.' },
  equator: { label: 'Equator Latitude', min: 0, max: 1, step: 0.02,
    desc: 'Which row of the map (as a fraction of its height) is hottest. Temperature falls off toward both the top and bottom edges, so this shifts the climate belts north or south.' },
  temp_band: { label: 'Temperate Width', min: 0.4, max: 2.0, step: 0.1,
    desc: 'How wide the warm/temperate zone is around the equator. Higher spreads warm climates toward the poles; lower squeezes them into a narrow tropical band with more tundra and ice.' },
  lapse: { label: 'Elevation Cooling', min: 0, max: 1.5, step: 0.05,
    desc: 'How strongly altitude cools temperature (environmental lapse rate). Higher makes mountains colder, pushing them toward taiga, tundra and snow regardless of latitude.' },
  wind_dir: { label: 'Prevailing Wind', min: 0, max: 360, step: 15,
    desc: 'Direction the prevailing wind blows from, in degrees (0 = north, 90 = east, 180 = south, 270 = west). Moist air sweeps across the map from this edge, raining out on windward mountain slopes and leaving a dry rain shadow to leeward.' },
  humidity: { label: 'Rainfall', min: 0, max: 2, step: 0.1,
    desc: 'Overall wetness of the air mass. Higher carries more moisture inland, pushing the world toward forests and rainforest; lower expands grassland, shrubland and desert.' },
  orographic: { label: 'Rain Shadow', min: 0, max: 2, step: 0.1,
    desc: 'How aggressively rising terrain wrings rain out of the passing air. Higher makes windward slopes much wetter and leeward basins much drier (strong rain shadow); lower spreads rain more evenly.' },
  aridity: { label: 'Aridity', min: 0, max: 1, step: 0.05,
    desc: 'How dry-skewed the world is overall, independent of rainfall. The moisture field is normally balanced so every biome band gets a roughly equal share; higher aridity pushes most land into the dry bands, growing sweeping deserts — hot deserts near the equator and cold deserts in dry temperate/polar interiors. 0 keeps the balanced spread.' },
  alpine_aridity: { label: 'Alpine Aridity', min: 0, max: 1, step: 0.05,
    desc: 'How strongly high ground dries out with altitude. The air has wrung out its moisture climbing the windward slopes, so summits sit in an altitude rain shadow — cold and arid. Higher turns peaks to bare rock with only patchy snow on the moister, gentler ground; 0 lets high ground stay as wet as its latitude allows, giving solid snow caps.' },
  rock_line: { label: 'Rock Line', min: 0.2, max: 0.95, step: 0.01,
    desc: 'Elevation (as a fraction of the land height above sea level) where bare rock starts replacing the underlying biome. Lower drops the rock line so more of the flanks turn to scree; higher keeps rock to the highest ground.' },
  snow_line: { label: 'Snow Line', min: 0.3, max: 1.0, step: 0.01,
    desc: 'Elevation (as a fraction of the land height above sea level) where snow starts capping the peaks. Lower brings the snowline down for whiter, more glaciated ranges; higher confines snow to the very tops. Cold latitudes already lower both lines automatically.' },
  alpine_blend: { label: 'Alpine Blend', min: 0.02, max: 0.5, step: 0.01,
    desc: 'How fast the terrain fades from biome to rock to snow. Small = a hard, sharp border at the rock/snow line; large = a slow, gradual melt over a wide band of altitude.' },
  river_moisture: { label: 'River Moisture', min: 0, max: 1, step: 0.05,
    desc: 'How much flowing rivers humidify their banks. Higher grows lush riparian corridors — gallery forests and greener valleys threading through otherwise dry land — while big trunks water a wider band than headwater creeks. 0 means rivers have no effect on the moisture map. (Seas and lakes always humidify their coasts.)' },
  forest_density: { label: 'Forest Texture', min: 0, max: 1, step: 0.05,
    desc: 'Strength of the forest canopy texture: how strongly wooded biomes are tinted toward a darker canopy color and mottled for a textured look. 0 = flat biome color; higher = richer, denser-looking woodland.' },
  biome_blend: { label: 'Biome Blending', min: 0, max: 1, step: 0.05,
    desc: 'How aggressively neighbouring biomes melt into one another. Biome colours are blended in climate space — interpolated across the Whittaker grid by each cell’s temperature & moisture — so transition width follows the climate gradient (wide where climate changes slowly, narrow where it changes fast). 0 = crisp biome regions; higher = soft, gradient-like transitions. Relief and coastlines stay sharp.' },
  relief: { label: 'Relief Exaggeration', min: 4, max: 30, step: 1,
    desc: 'Vertical exaggeration of the shaded relief. Higher makes mountains read taller and slopes pop dramatically in all views. Purely visual — it shades the same heightmap more steeply, it does not change the terrain data.' },
  hillshade_strength: { label: 'Hillshade Strength', min: 0.5, max: 3.0, step: 0.1,
    desc: 'Contrast of the shaded relief around mid-grey. Higher deepens shadows and brightens highlights for punchier hillshading; lower flattens it to a softer, more uniform shade. Purely visual.' },
  coastal_smooth_width: { label: 'Smoothing Reach', min: 0.02, max: 0.25, step: 0.01,
    desc: 'How far inland (in height units above sea level) the smoothing fades out. Larger values ease more of the low coastal plains, not just the immediate shoreline.' },
  coastal_smooth_strength: { label: 'Smoothing Strength', min: 0, max: 1, step: 0.05,
    desc: 'How strongly the sea floor and coast are blurred. 1 fully smooths everything below the waterline; lower keeps some of the original seabed texture.' },
  crater_count: { label: 'Crater Count', min: 1, max: 60, step: 1,
    desc: 'Number of impact craters to stamp on the map. Craters can overlap; smaller craters punched after larger ones are sorted to stamp last.' },
  crater_min_radius: { label: 'Min Radius', min: 0.01, max: 0.15, step: 0.01,
    desc: 'Smallest crater radius as a fraction of the map size. 0.02 = 2 % of the shorter dimension.' },
  crater_max_radius: { label: 'Max Radius', min: 0.02, max: 0.30, step: 0.01,
    desc: 'Largest crater radius as a fraction of the map size. Wide range between min and max produces mixed-size fields.' },
  crater_depth: { label: 'Bowl Depth', min: 0.02, max: 0.4, step: 0.01,
    desc: 'Depth of the crater bowl in height units. Higher values dig more dramatically into the terrain.' },
  crater_rim_height: { label: 'Rim Height', min: 0.005, max: 0.2, step: 0.005,
    desc: 'Height of the raised rim around each crater. The rim is tallest at the edge and tapers inward and outward.' },
  crater_ejecta_falloff: { label: 'Ejecta Spread', min: 0.5, max: 10.0, step: 0.5,
    desc: 'How quickly the ejecta blanket thins with distance from the rim. Low = wide ejecta field (ancient/eroded). High = tight blanket (fresh impact).' },
};

// Underground/cave generation — defaults mirror backend caves.CaveParams.
const CAVE_DEFAULTS = {
  seed: -1,
  resolution: 384,
  cavern_density: 0.22,
  cavern_size: 0.5,
  tunnel_width: 0.5,
  tunnel_windiness: 0.5,
  extra_tunnels: 0.4,
  ca_iterations: 4,
  water_level: 0.28,
  lava_amount: 0.5,
  crystal_amount: 0.5,
  ice_amount: 0.3,
  biome_blend: 0.6,
  river_density: 0.5,
  relief: 14.0,
  hillshade_strength: 1.6,
};

const CAVE_SLIDER_META = {
  resolution: { label: 'Resolution', min: 128, max: 1024, step: 64,
    desc: 'Grid size of the cave map. Higher = more detail but slower to carve.' },
  cavern_density: { label: 'Cavern Density', min: 0.05, max: 0.6, step: 0.01,
    desc: 'Fraction of the map carved into open caverns. Higher = more void, less solid rock.' },
  cavern_size: { label: 'Cavern Size', min: 0, max: 1, step: 0.05,
    desc: 'Scale of the cavern blobs. Low = many small pockets; high = fewer, larger chambers.' },
  tunnel_width: { label: 'Tunnel Width', min: 0, max: 1, step: 0.05,
    desc: 'Thickness of the corridors that connect caverns. Narrow = tight passages; wide = broad halls.' },
  tunnel_windiness: { label: 'Tunnel Windiness', min: 0, max: 1, step: 0.05,
    desc: 'How much tunnels wander through soft rock instead of taking the straight route between caverns.' },
  extra_tunnels: { label: 'Extra Tunnels', min: 0, max: 1, step: 0.05,
    desc: 'Loop tunnels beyond the minimum needed to connect everything. Higher = more interconnected, mazelike networks.' },
  ca_iterations: { label: 'Smoothing Passes', min: 0, max: 8, step: 1,
    desc: 'Cellular-automata passes that smooth jagged cave walls into organic shapes. More = rounder caverns.' },
  water_level: { label: 'Water Level', min: 0, max: 0.95, step: 0.01,
    desc: 'Floor height below which standing water pools. Higher floods more of the deep cavern floor.' },
  lava_amount: { label: 'Lava Tubes', min: 0, max: 1, step: 0.05,
    desc: 'Abundance of hot lava-tube caverns in the deepest, driest cells.' },
  crystal_amount: { label: 'Crystal Caverns', min: 0, max: 1, step: 0.05,
    desc: 'Abundance of crystal-lined caverns scattered through the network.' },
  ice_amount: { label: 'Ice Caves', min: 0, max: 1, step: 0.05,
    desc: 'Abundance of frozen caves in cold, dry pockets.' },
  biome_blend: { label: 'Biome Blending', min: 0, max: 1, step: 0.05,
    desc: 'How softly the underground biomes (grotto, crystal, lava, ice) melt into one another.' },
  river_density: { label: 'River Density', min: 0, max: 1, step: 0.05,
    desc: 'Abundance of flowing underground rivers carved across the cavern floor.' },
  relief: { label: 'Relief Exaggeration', min: 4, max: 30, step: 1,
    desc: 'Vertical exaggeration of the floor relief shading. Purely visual.' },
  hillshade_strength: { label: 'Hillshade Strength', min: 0.5, max: 3.0, step: 0.1,
    desc: 'Contrast of the floor relief shading. Purely visual.' },
};

function InfoTip({ text }) {
  return (
    <span className="relative inline-flex group align-middle">
      <span className="ml-1 w-3.5 h-3.5 inline-flex items-center justify-center rounded-full border border-gray-600 text-gray-500 text-[9px] leading-none cursor-help group-hover:border-purple-400 group-hover:text-purple-300">
        i
      </span>
      <span
        role="tooltip"
        className="pointer-events-none absolute left-1/2 bottom-full z-20 mb-1.5 w-56 -translate-x-1/2 rounded-md border border-gray-700 bg-gray-950 px-2.5 py-1.5 text-[11px] leading-snug text-gray-300 opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}

// Collapsible, named control category. Starts collapsed by default.
function Section({ title, badge, children }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-gray-800 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-gray-200 bg-gray-900/60 hover:bg-gray-800/60"
      >
        <span className="flex items-center gap-2">
          {title}
          {badge}
        </span>
        <span className={`text-gray-500 text-[10px] transition-transform ${open ? 'rotate-90' : ''}`}>
          ▶
        </span>
      </button>
      {open && (
        <div className="p-3 space-y-3 border-t border-gray-800">{children}</div>
      )}
    </div>
  );
}

// Small coloured dot shown on a collapsed section header when its feature is on.
function ActiveDot({ className }) {
  return <span className={`w-1.5 h-1.5 rounded-full ${className}`} />;
}

function Slider({ k, params, setParam, accentClass = 'accent-purple-500' }) {
  const meta = SLIDER_META[k];
  return (
    <div>
      <div className="flex justify-between text-xs text-gray-400 mb-1">
        <span className="flex items-center">
          {meta.label}
          {meta.desc && <InfoTip text={meta.desc} />}
        </span>
        <span className="text-gray-300 tabular-nums">{params[k]}</span>
      </div>
      <input
        type="range"
        min={meta.min}
        max={meta.max}
        step={meta.step}
        value={params[k]}
        onChange={(e) => setParam(k, parseFloat(e.target.value))}
        className={`w-full ${accentClass}`}
      />
    </div>
  );
}

function Toggle({ k, label, params, setParam, desc, accentClass = 'accent-purple-500' }) {
  return (
    <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer">
      <input
        type="checkbox"
        checked={params[k]}
        onChange={(e) => setParam(k, e.target.checked)}
        className={accentClass}
      />
      {label}
      {desc && <InfoTip text={desc} />}
    </label>
  );
}

export default function TerrainLab({ onBack }) {
  const [terrainMode, setTerrainMode] = useState('surface'); // 'surface' | 'underground'
  const [params, setParams] = useState(DEFAULTS);
  const [result, setResult] = useState(null);
  const [view, setView] = useState('elevation'); // 'elevation' | 'hillshade' | 'biome'
  const [loading, setLoading] = useState(false);
  const [rebiomeLoading, setRebiomeLoading] = useState(false); // fast biome re-derive
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(false);
  const [paletteCopied, setPaletteCopied] = useState(false);
  const [live, setLive] = useState(true);   // stream work-in-progress frames
  const [frame, setFrame] = useState(null);  // latest preview frame while generating
  const [rev, setRev] = useState(null);      // cache-bust token after a biome re-derive

  const [palette, setPalette] = useState(null); // { realistic:[{id,name,color}], fantasy:[...] }
  // User colour overrides per mode: { realistic: { [id]: '#rrggbb' }, fantasy: {...} }.
  const [biomeColors, setBiomeColors] = useState(() => {
    try { return JSON.parse(storage.getItem(BIOME_COLORS_KEY)) || {}; }
    catch { return {}; }
  });

  const setParam = (key, value) => setParams((p) => ({ ...p, [key]: value }));

  // Fetch the editable palettes once, then seed any biome colour not already set
  // (from localStorage) with its source-palette default.
  useEffect(() => {
    let cancelled = false;
    api.getTerrainPalette().then((pal) => {
      if (cancelled) return;
      setPalette(pal);
      setBiomeColors((prev) => {
        const next = { ...prev };
        for (const mode of ['realistic', 'fantasy']) {
          const m = { ...(next[mode] || {}) };
          for (const b of pal[mode]) {
            if (!m[b.id]) m[b.id] = rgbToHex(b.color);
          }
          next[mode] = m;
        }
        return next;
      });
    }).catch(() => { /* palette is optional; editor just stays empty */ });
    return () => { cancelled = true; };
  }, []);

  // Persist colour edits so a reload restores the experiment.
  useEffect(() => {
    try { storage.setItem(BIOME_COLORS_KEY, JSON.stringify(biomeColors)); }
    catch { /* ignore quota/availability errors */ }
  }, [biomeColors]);

  const mode = params.biome_mode;
  const modeBiomes = useMemo(() => palette?.[mode] || [], [palette, mode]);

  const setBiomeColor = (id, hex) =>
    setBiomeColors((c) => ({ ...c, [mode]: { ...(c[mode] || {}), [id]: hex } }));

  const resetColors = () => {
    if (!palette) return;
    setBiomeColors((c) => ({
      ...c,
      [mode]: Object.fromEntries(modeBiomes.map((b) => [b.id, rgbToHex(b.color)])),
    }));
  };

  // Only colours that differ from the source-palette default are sent to the
  // backend (as id -> [r,g,b]); null when nothing is customised.
  const colorOverrides = useMemo(() => {
    if (!palette) return null;
    const out = {};
    for (const b of modeBiomes) {
      const cur = biomeColors[mode]?.[b.id];
      if (cur && cur.toLowerCase() !== rgbToHex(b.color).toLowerCase()) {
        out[b.id] = hexToRgb(cur);
      }
    }
    return Object.keys(out).length ? out : null;
  }, [palette, modeBiomes, biomeColors, mode]);

  const copyPalette = () => {
    if (!palette) return;
    const rows = modeBiomes.map((b) => {
      const [r, g, bl] = hexToRgb(biomeColors[mode]?.[b.id] || rgbToHex(b.color));
      return `  ${b.id}: ("${b.name}", (${r}, ${g}, ${bl})),`;
    });
    const jsonMap = Object.fromEntries(
      modeBiomes.map((b) => [b.id, hexToRgb(biomeColors[mode]?.[b.id] || rgbToHex(b.color))]));
    const text =
      `# ${mode} palette — Terrain Lab export (id: ("Name", (r, g, b)))\n` +
      `${rows.join('\n')}\n\n` +
      `# id -> [r, g, b]\n${JSON.stringify(jsonMap)}`;
    navigator.clipboard?.writeText(text);
    setPaletteCopied(true);
    setTimeout(() => setPaletteCopied(false), 1200);
  };

  const copySeed = (s) => {
    navigator.clipboard?.writeText(String(s));
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  const generate = useCallback(async () => {
    setLoading(true);
    setError(null);
    setFrame(null);
    try {
      const payload = { ...params, colors: colorOverrides };
      const res = live
        ? await api.generateTerrainStream(payload, setFrame)
        : await api.generateTerrain(payload);
      setResult(res);
      setRev(null); // new run_id already cache-busts; clear any stale re-derive token
    } catch (e) {
      setError(e.message || 'Generation failed');
    } finally {
      setLoading(false);
      setFrame(null);
    }
  }, [params, live, colorOverrides]);

  // Fast path: recompute biomes (+ climate + colours) on the existing eroded
  // terrain without re-running heightmap/erosion/rivers. Requires a prior run.
  const regenerateBiomes = useCallback(async () => {
    if (!result) return;
    setRebiomeLoading(true);
    setError(null);
    try {
      const sub = { colors: colorOverrides,
        seed: result.params?.seed ?? params.seed };
      for (const k of BIOME_PARAM_KEYS) sub[k] = params[k];
      const res = await api.rederiveBiomes(result.run_id, sub);
      setResult((prev) => ({ ...prev, images: { ...prev.images, ...res.images } }));
      setRev(res.rev);
      setView('biome');
    } catch (e) {
      setError(e.status === 404
        ? 'Run a full Generate once before regenerating biomes (no stored terrain for this run).'
        : (e.message || 'Biome re-derive failed'));
    } finally {
      setRebiomeLoading(false);
    }
  }, [result, params, colorOverrides]);

  const randomizeSeed = () => setParam('seed', Math.floor(Math.random() * 1_000_000));

  // Cache-bust the image so re-generating (new run_id) or re-deriving biomes
  // (same run_id, new ``rev`` token) still refreshes the <img>.
  const imgSrc = result
    ? `${result.images[view]}?t=${rev || result.run_id}`
    : null;

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 text-gray-100 p-6">
      <div className="max-w-6xl mx-auto">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold flex items-center gap-2">
              🗺️ Experimental World Visualization
            </h1>
            <p className="text-sm text-gray-500">
              Heightmap → tectonics → erosion. Not wired into world generation yet.
            </p>
          </div>
          <button
            onClick={onBack}
            className="text-gray-400 hover:text-gray-200 text-sm border border-gray-700 rounded px-3 py-1.5"
          >
            ← Back
          </button>
        </div>

        {/* Surface / Underground top tabs */}
        <div className="flex items-center gap-2 mb-4">
          {[['surface', '🌍 Surface'], ['underground', '🕳️ Underground']].map(([k, label]) => (
            <button
              key={k}
              onClick={() => setTerrainMode(k)}
              className={`text-sm px-4 py-1.5 rounded-lg border ${
                terrainMode === k
                  ? 'bg-purple-600/40 border-purple-500/60 text-purple-100'
                  : 'border-gray-700 text-gray-400 hover:border-gray-600'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {terrainMode === 'underground' && <UndergroundLab />}

        {terrainMode === 'surface' && (
        <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6">
          {/* Controls */}
          <div className="space-y-2.5 bg-gray-900/60 border border-gray-800 rounded-xl p-4">

            <Section title="Base Terrain">
              <div className="flex items-end gap-2">
                <label className="flex-1 text-xs text-gray-400">
                  Seed
                  <InfoTip text="Random seed for this world. The same seed with the same parameters always regenerates the identical map. Set to -1 to roll a fresh random seed on every generation — the seed actually used is shown under the map so you can copy and reuse it." />
                  <input
                    type="number"
                    value={params.seed}
                    onChange={(e) => setParam('seed', parseInt(e.target.value) || 0)}
                    className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
                  />
                </label>
                <button
                  onClick={randomizeSeed}
                  className="text-xs border border-gray-700 rounded px-2 py-1.5 hover:bg-gray-800"
                  title="Random seed"
                >
                  🎲
                </button>
              </div>
              <Slider k="resolution" params={params} setParam={setParam} />
              <Slider k="octaves" params={params} setParam={setParam} />
              <Slider k="warp" params={params} setParam={setParam} />
              <Slider k="island" params={params} setParam={setParam} />
            </Section>

            <Section
              title="Mountains"
              badge={params.spline_ridges > 0 ? <ActiveDot className="bg-amber-400" /> : null}
            >
              <Slider k="mountain_strength" params={params} setParam={setParam} />
              <Slider k="mountain_coverage" params={params} setParam={setParam} />
              <Slider k="mountain_sharpness" params={params} setParam={setParam} />
              <Slider k="redistribution" params={params} setParam={setParam} />
              <div className="border-t border-gray-800 pt-3 space-y-3">
                <Slider k="spline_ridges" params={params} setParam={setParam} accentClass="accent-amber-500" />
                {params.spline_ridges > 0 && (
                  <div className="space-y-3 pl-1 border-l border-amber-900/40">
                    <Slider k="spline_ridge_strength" params={params} setParam={setParam} accentClass="accent-amber-500" />
                    <Slider k="spline_ridge_width" params={params} setParam={setParam} accentClass="accent-amber-500" />
                    <Slider k="spline_ridge_length" params={params} setParam={setParam} accentClass="accent-amber-500" />
                  </div>
                )}
              </div>
            </Section>

            <Section
              title="Erosion & Hydrology"
              badge={params.hydrology_model === 'momentum' && <ActiveDot className="bg-blue-400" />}
            >
              <label className="block text-xs text-gray-400">
                Hydrology Model
                <InfoTip text="How water shapes the land. 'Momentum' simulates particles whose stored momentum steers later flow, so meanders, oxbows and channels emerge physically in the heightmap and drive the rivers directly (SimpleHydrology approach). 'Droplet' is the classic independent-droplet carver with geometric meandering added afterward." />
                <select
                  value={params.hydrology_model}
                  onChange={(e) => setParam('hydrology_model', e.target.value)}
                  className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
                >
                  <option value="momentum">Momentum (emergent meanders)</option>
                  <option value="droplet">Droplet (classic + geometric)</option>
                </select>
              </label>
              <Slider k="thermal_iterations" params={params} setParam={setParam} />
              <Slider k="erosion_strength" params={params} setParam={setParam} />

              {params.hydrology_model === 'momentum' ? (
                <div className="space-y-3 pl-1 border-l border-blue-900/40">
                  <Slider k="momentum_iterations" params={params} setParam={setParam} accentClass="accent-blue-500" />
                  <Slider k="momentum_particles" params={params} setParam={setParam} accentClass="accent-blue-500" />
                  <Slider k="momentum_transfer" params={params} setParam={setParam} accentClass="accent-blue-500" />
                  <Slider k="discharge_alpha" params={params} setParam={setParam} accentClass="accent-blue-500" />
                </div>
              ) : (
                <Slider k="droplets" params={params} setParam={setParam} />
              )}

              <label className="block text-xs text-gray-400">
                Compute Backend
                <InfoTip text="Which compute engine to use. 'auto' picks the fast numba kernel when available, otherwise the pure-numpy fallback. The momentum model has no true-meander numpy fallback — it approximates with discharge-weighted diffusion." />
                <select
                  value={params.erosion_backend}
                  onChange={(e) => setParam('erosion_backend', e.target.value)}
                  className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
                >
                  <option value="auto">auto (numba if available)</option>
                  <option value="numba">numba</option>
                  <option value="numpy">numpy (fallback)</option>
                </select>
              </label>
            </Section>

            <Section
              title="Sea & Coast"
              badge={params.coastal_smooth && <ActiveDot className="bg-cyan-400" />}
            >
              <Slider k="sea_level" params={params} setParam={setParam} />
              <div className="border-t border-gray-800 pt-3">
                <Toggle
                  k="coastal_smooth"
                  label="Smooth Coast & Seabed"
                  params={params}
                  setParam={setParam}
                  accentClass="accent-cyan-500"
                  desc="A pass that blurs the ocean floor into a smooth shelf and eases low coastal land into gentle slopes instead of sharp sea cliffs. Deep inland terrain is untouched. Applied before rivers and lakes."
                />
                {params.coastal_smooth && (
                  <div className="mt-3 space-y-3 pl-1 border-l border-cyan-900/40">
                    <Slider k="coastal_smooth_width" params={params} setParam={setParam} accentClass="accent-cyan-500" />
                    <Slider k="coastal_smooth_strength" params={params} setParam={setParam} accentClass="accent-cyan-500" />
                  </div>
                )}
              </div>
            </Section>

            <Section
              title="Rivers & Lakes"
              badge={(params.rivers || params.lakes) && <ActiveDot className="bg-purple-400" />}
            >
              <Toggle
                k="rivers"
                label="Generate Rivers"
                params={params}
                setParam={setParam}
                desc="Compute a river network (depression fill → flow accumulation) and overlay it. Rivers start in the mountains and widen toward the coast."
              />
              <Slider k="river_density" params={params} setParam={setParam} />
              <Slider k="river_carve" params={params} setParam={setParam} />
              <Slider k="river_meander" params={params} setParam={setParam} />

              <div className="border-t border-gray-800 pt-3 space-y-3">
                <Toggle
                  k="lakes"
                  label="Generate Lakes"
                  params={params}
                  setParam={setParam}
                  desc="Keep large/deep depressions as flat lakes instead of filling them. Rivers flow into a lake and the overflow continues from its spill point."
                />
                <Slider k="lake_min_area" params={params} setParam={setParam} />
                <Slider k="lake_min_depth" params={params} setParam={setParam} />
                <Toggle
                  k="breach"
                  label="Breach Shallow Basins"
                  params={params}
                  setParam={setParam}
                  desc="Carve a drainage notch through the rim of shallow depressions so they drain as natural valleys (preserving slope) instead of being flat-filled. See Breach Depth."
                />
                <Slider k="breach_max_depth" params={params} setParam={setParam} />
              </div>

              <div className="border-t border-gray-800 pt-3 space-y-3">
                <Toggle
                  k="deltas"
                  label="River Deltas"
                  params={params}
                  setParam={setParam}
                  desc="Build L-system distributary deltas where large rivers meet the sea: a fan of deposited land (sediment) with branching channels — a bird-foot delta. See Delta Size."
                />
                <Slider k="delta_size" params={params} setParam={setParam} />
              </div>
            </Section>

            <Section
              title="Meteoric Impacts"
              badge={params.craters && <ActiveDot className="bg-orange-400" />}
            >
              <Toggle
                k="craters"
                label="Enable Impacts"
                params={params}
                setParam={setParam}
                accentClass="accent-orange-500"
                desc="Stamp impact craters onto the terrain. 'Ancient' craters are placed before erosion so their rims weather down; 'Fresh' craters are placed after erosion with sharp rims intact, and their bowls may fill as lakes."
              />
              {params.craters && (
                <div className="space-y-3 pl-1 border-l border-orange-900/40">
                  <label className="block text-xs text-gray-400">
                    Impact Age
                    <InfoTip text="Ancient: craters stamped before erosion — rims erode, ejecta blends in. Fresh: craters stamped after erosion — sharp rims, possible crater lakes." />
                    <select
                      value={params.crater_age}
                      onChange={(e) => setParam('crater_age', e.target.value)}
                      className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
                    >
                      <option value="ancient">Ancient (pre-erosion)</option>
                      <option value="fresh">Fresh (post-erosion)</option>
                    </select>
                  </label>
                  <Slider k="crater_count" params={params} setParam={setParam} accentClass="accent-orange-500" />
                  <Slider k="crater_min_radius" params={params} setParam={setParam} accentClass="accent-orange-500" />
                  <Slider k="crater_max_radius" params={params} setParam={setParam} accentClass="accent-orange-500" />
                  <Slider k="crater_depth" params={params} setParam={setParam} accentClass="accent-orange-500" />
                  <Slider k="crater_rim_height" params={params} setParam={setParam} accentClass="accent-orange-500" />
                  <Slider k="crater_ejecta_falloff" params={params} setParam={setParam} accentClass="accent-orange-500" />
                </div>
              )}
            </Section>

            <Section
              title="Biomes & Vegetation"
              badge={params.biome_mode === 'fantasy' || params.fantasy_overlay
                ? <ActiveDot className="bg-fuchsia-400" /> : null}
            >
              <label className="block text-xs text-gray-400">
                Biome Style
                <InfoTip text="How biomes are labelled and coloured. Climate classification (temperature × moisture) is identical either way — 'Realistic' uses earthly biomes and palettes; 'Fantasy' remaps the same zones to exotic ones (ashlands, enchanted groves, fungal forests, crystal fields…)." />
                <select
                  value={params.biome_mode}
                  onChange={(e) => setParam('biome_mode', e.target.value)}
                  className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
                >
                  <option value="realistic">Realistic</option>
                  <option value="fantasy">Fantasy</option>
                </select>
              </label>
              <Toggle
                k="fantasy_overlay"
                label="Fantasy Overlay Patches"
                params={params}
                setParam={setParam}
                accentClass="accent-fuchsia-500"
                desc="Stamp special fantasy regions (corrupted wastes, arcane groves) over the base biomes via noise masks. Works on top of either realistic or fantasy palettes."
              />
              <div className="border-t border-gray-800 pt-3 space-y-3">
                <Slider k="biome_blend" params={params} setParam={setParam} />
              </div>
              <div className="border-t border-gray-800 pt-3 space-y-3">
                <Slider k="equator" params={params} setParam={setParam} />
                <Slider k="temp_band" params={params} setParam={setParam} />
                <Slider k="lapse" params={params} setParam={setParam} />
                <Slider k="wind_dir" params={params} setParam={setParam} />
                <Slider k="humidity" params={params} setParam={setParam} />
                <Slider k="orographic" params={params} setParam={setParam} />
                <Slider k="aridity" params={params} setParam={setParam} />
                <Slider k="alpine_aridity" params={params} setParam={setParam} />
                <Slider k="rock_line" params={params} setParam={setParam} />
                <Slider k="snow_line" params={params} setParam={setParam} />
                <Slider k="alpine_blend" params={params} setParam={setParam} />
                <Slider k="river_moisture" params={params} setParam={setParam} />
              </div>
              <div className="border-t border-gray-800 pt-3 space-y-3">
                <Toggle
                  k="forests"
                  label="Forest Canopy"
                  params={params}
                  setParam={setParam}
                  desc="Render wooded biomes with a soft canopy texture — a darker, mottled tint over forest regions — instead of a flat colour band. Shown in the Biomes view."
                />
                <Slider k="forest_density" params={params} setParam={setParam} />
              </div>

              <div className="border-t border-gray-800 pt-3 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-gray-300 flex items-center">
                    Biome Colors
                    <InfoTip text="Override the colour of each biome for the active style. Edits preview via Regenerate Biomes (fast — no erosion) or the next full Generate. Colours are interpolated in climate space, so neighbouring biomes blend toward these. Saved locally; use Copy to export." />
                  </span>
                  <div className="flex items-center gap-1.5">
                    <button
                      onClick={copyPalette}
                      disabled={!palette}
                      className="border border-gray-700 rounded px-1.5 py-0.5 text-[10px] hover:bg-gray-800 hover:text-gray-200 disabled:opacity-40"
                      title="Copy the active palette as a paste-ready snippet for biomes.py"
                    >
                      {paletteCopied ? 'copied' : 'copy'}
                    </button>
                    <button
                      onClick={resetColors}
                      disabled={!palette}
                      className="border border-gray-700 rounded px-1.5 py-0.5 text-[10px] hover:bg-gray-800 hover:text-gray-200 disabled:opacity-40"
                      title="Reset colours to the source-palette defaults for this style"
                    >
                      reset
                    </button>
                  </div>
                </div>
                {palette ? (
                  <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
                    {modeBiomes.map((b) => (
                      <label key={b.id} className="flex items-center gap-2 text-[11px] text-gray-400 cursor-pointer">
                        <input
                          type="color"
                          value={biomeColors[mode]?.[b.id] || rgbToHex(b.color)}
                          onChange={(e) => setBiomeColor(b.id, e.target.value)}
                          className="w-5 h-5 rounded border border-gray-700 bg-transparent cursor-pointer p-0"
                          title={b.name}
                        />
                        <span className="truncate">{b.name}</span>
                      </label>
                    ))}
                  </div>
                ) : (
                  <p className="text-[11px] text-gray-600">Loading palette…</p>
                )}
              </div>
            </Section>

            <Section title="Rendering">
              <Slider k="relief" params={params} setParam={setParam} />
              <Slider k="hillshade_strength" params={params} setParam={setParam} />
            </Section>

            <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer mt-1">
              <input
                type="checkbox"
                checked={live}
                onChange={(e) => setLive(e.target.checked)}
                className="accent-purple-500"
              />
              Live preview
              <InfoTip text="Stream the terrain as it forms — base shape, mountains, then each erosion pass — so you can watch rivers carve in real time. Slightly slower due to extra rendering; turn off for a single final image." />
            </label>
            <button
              onClick={generate}
              disabled={loading || rebiomeLoading}
              className="w-full bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded py-2 text-sm font-medium mt-1"
            >
              {loading ? 'Generating…' : 'Generate'}
            </button>
            <button
              onClick={regenerateBiomes}
              disabled={!result || loading || rebiomeLoading}
              title={result
                ? 'Recompute biomes, climate and colours on the current terrain — fast, no erosion'
                : 'Generate a world first, then regenerate just its biomes'}
              className="w-full bg-fuchsia-700/80 hover:bg-fuchsia-600 disabled:opacity-40 rounded py-2 text-sm font-medium flex items-center justify-center gap-2"
            >
              {rebiomeLoading ? 'Regenerating biomes…' : '🎨 Regenerate Biomes'}
            </button>
            <button
              onClick={() => setParams(DEFAULTS)}
              className="w-full text-xs text-gray-500 hover:text-gray-300"
            >
              Reset to defaults
            </button>

            {error && (
              <p className="text-xs text-red-400 border border-red-900/50 bg-red-950/30 rounded p-2">
                {error}
              </p>
            )}
          </div>

          {/* Preview */}
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              {[
                ['elevation', 'Elevation'],
                ['hillshade', 'Hillshade'],
                ['biome', 'Biomes'],
                ['temperature', 'Temperature'],
                ['moisture', 'Moisture'],
              ].map(([key, label]) => (
                <button
                  key={key}
                  onClick={() => setView(key)}
                  className={`text-xs px-3 py-1.5 rounded border ${
                    view === key
                      ? 'bg-purple-600/40 border-purple-500/50 text-purple-200'
                      : 'border-gray-700 text-gray-400 hover:border-gray-600'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            <div className="relative aspect-square w-full bg-gray-900/60 border border-gray-800 rounded-xl flex items-center justify-center overflow-hidden">
              {loading && frame ? (
                <>
                  <img
                    src={`data:image/png;base64,${frame.image}`}
                    alt={frame.label}
                    className="w-full h-full object-contain"
                    style={{ imageRendering: 'pixelated' }}
                  />
                  <div className="absolute inset-x-0 bottom-0 bg-gray-950/80 px-3 py-2">
                    <div className="flex items-center justify-between text-[11px] text-gray-300 mb-1">
                      <span>{frame.label}</span>
                      <span className="tabular-nums">
                        {frame.total ? `${frame.done}/${frame.total} · ` : ''}
                        {Math.round((frame.frac || 0) * 100)}%
                      </span>
                    </div>
                    <div className="h-1 w-full rounded bg-gray-800 overflow-hidden">
                      <div
                        className="h-full bg-purple-500 transition-[width] duration-150"
                        style={{ width: `${Math.round((frame.frac || 0) * 100)}%` }}
                      />
                    </div>
                  </div>
                </>
              ) : imgSrc ? (
                <img
                  src={imgSrc}
                  alt={view}
                  className="w-full h-full object-contain"
                  style={{ imageRendering: 'pixelated' }}
                />
              ) : (
                <span className="text-gray-600 text-sm">
                  {loading ? 'Generating…' : 'Press Generate to render a world'}
                </span>
              )}
            </div>

            {result && (
              <div className="text-xs text-gray-500 bg-gray-900/60 border border-gray-800 rounded-lg p-3 font-mono">
                <div className="mb-1.5 flex items-center gap-2 text-gray-400">
                  <span>seed</span>
                  <span className="text-gray-100 tabular-nums select-all">
                    {result.params?.seed ?? result.stats.seed}
                  </span>
                  <button
                    onClick={() => copySeed(result.params?.seed ?? result.stats.seed)}
                    className="border border-gray-700 rounded px-1.5 py-0.5 text-[10px] hover:bg-gray-800 hover:text-gray-200"
                    title="Copy seed to clipboard"
                  >
                    {copied ? 'copied' : 'copy'}
                  </button>
                  <button
                    onClick={() => setParam('seed', result.params?.seed ?? result.stats.seed)}
                    className="border border-gray-700 rounded px-1.5 py-0.5 text-[10px] hover:bg-gray-800 hover:text-gray-200"
                    title="Load this seed into the Seed field so the next generation reuses it"
                  >
                    use
                  </button>
                </div>
                <div className="mb-1 text-gray-400">
                  run {result.run_id} · backend {result.stats.erosion_backend} · land{' '}
                  {(result.stats.land_fraction * 100).toFixed(0)}%
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-0.5">
                  {Object.entries(result.stats)
                    .filter(([k]) => k.endsWith('_s'))
                    .map(([k, v]) => (
                      <span key={k}>
                        {k.replace('_s', '')}: {v}s
                      </span>
                    ))}
                </div>
              </div>
            )}
          </div>
        </div>
        )}
      </div>
    </div>
  );
}


function CaveSlider({ k, params, setParam, accentClass = 'accent-amber-500' }) {
  const meta = CAVE_SLIDER_META[k];
  return (
    <div>
      <div className="flex justify-between text-xs text-gray-400 mb-1">
        <span className="flex items-center">
          {meta.label}
          {meta.desc && <InfoTip text={meta.desc} />}
        </span>
        <span className="text-gray-300 tabular-nums">{params[k]}</span>
      </div>
      <input
        type="range"
        min={meta.min}
        max={meta.max}
        step={meta.step}
        value={params[k]}
        onChange={(e) => setParam(k, parseFloat(e.target.value))}
        className={`w-full ${accentClass}`}
      />
    </div>
  );
}

// Underground/cave panel: its own params + single-shot generation + top-down
// preview. Self-contained so the Surface lab above is untouched.
function UndergroundLab() {
  const [params, setParams] = useState(CAVE_DEFAULTS);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(false);

  const setParam = (key, value) => setParams((p) => ({ ...p, [key]: value }));
  const randomizeSeed = () => setParam('seed', Math.floor(Math.random() * 1_000_000));

  const generate = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.generateCaveTerrain(params);
      setResult(res);
    } catch (e) {
      setError(e.message || 'Cave generation failed');
    } finally {
      setLoading(false);
    }
  }, [params]);

  const copySeed = (s) => {
    navigator.clipboard?.writeText(String(s));
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  const imgSrc = result ? `${result.images.cave}?t=${result.run_id}` : null;
  const seed = result ? (result.params?.seed ?? result.stats.seed) : null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6">
      {/* Controls */}
      <div className="space-y-2.5 bg-gray-900/60 border border-gray-800 rounded-xl p-4">
        <Section title="Cavern Shape">
          <div className="flex items-end gap-2">
            <label className="flex-1 text-xs text-gray-400">
              Seed
              <InfoTip text="Random seed for this cave system. The same seed with the same parameters always regenerates the identical layout. Set to -1 to roll a fresh seed each run — the seed used is shown under the map." />
              <input
                type="number"
                value={params.seed}
                onChange={(e) => setParam('seed', parseInt(e.target.value) || 0)}
                className="mt-1 w-full bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-gray-100"
              />
            </label>
            <button
              onClick={randomizeSeed}
              className="text-xs border border-gray-700 rounded px-2 py-1.5 hover:bg-gray-800"
              title="Random seed"
            >
              🎲
            </button>
          </div>
          <CaveSlider k="resolution" params={params} setParam={setParam} />
          <CaveSlider k="cavern_density" params={params} setParam={setParam} />
          <CaveSlider k="cavern_size" params={params} setParam={setParam} />
          <CaveSlider k="ca_iterations" params={params} setParam={setParam} />
        </Section>

        <Section title="Tunnels">
          <CaveSlider k="tunnel_width" params={params} setParam={setParam} />
          <CaveSlider k="tunnel_windiness" params={params} setParam={setParam} />
          <CaveSlider k="extra_tunnels" params={params} setParam={setParam} />
        </Section>

        <Section title="Water">
          <CaveSlider k="water_level" params={params} setParam={setParam} />
          <CaveSlider k="river_density" params={params} setParam={setParam} />
        </Section>

        <Section title="Cave Biomes">
          <CaveSlider k="lava_amount" params={params} setParam={setParam} />
          <CaveSlider k="crystal_amount" params={params} setParam={setParam} />
          <CaveSlider k="ice_amount" params={params} setParam={setParam} />
          <CaveSlider k="biome_blend" params={params} setParam={setParam} />
        </Section>

        <Section title="Rendering">
          <CaveSlider k="relief" params={params} setParam={setParam} />
          <CaveSlider k="hillshade_strength" params={params} setParam={setParam} />
        </Section>

        <button
          onClick={generate}
          disabled={loading}
          className="w-full bg-purple-600 hover:bg-purple-500 disabled:opacity-50 rounded py-2 text-sm font-medium mt-1"
        >
          {loading ? 'Carving…' : 'Generate'}
        </button>
        <button
          onClick={() => setParams(CAVE_DEFAULTS)}
          className="w-full text-xs text-gray-500 hover:text-gray-300"
        >
          Reset to defaults
        </button>

        {error && (
          <p className="text-xs text-red-400 border border-red-900/50 bg-red-950/30 rounded p-2">
            {error}
          </p>
        )}
      </div>

      {/* Preview */}
      <div className="space-y-3">
        <div className="relative aspect-square w-full bg-gray-900/60 border border-gray-800 rounded-xl flex items-center justify-center overflow-hidden">
          {imgSrc ? (
            <img
              src={imgSrc}
              alt="underground"
              className="w-full h-full object-contain"
              style={{ imageRendering: 'pixelated' }}
            />
          ) : (
            <span className="text-gray-600 text-sm">
              {loading ? 'Carving…' : 'Press Generate to carve an underground'}
            </span>
          )}
        </div>

        {result && (
          <div className="text-xs text-gray-500 bg-gray-900/60 border border-gray-800 rounded-lg p-3 font-mono">
            <div className="mb-1.5 flex items-center gap-2 text-gray-400">
              <span>seed</span>
              <span className="text-gray-100 tabular-nums select-all">{seed}</span>
              <button
                onClick={() => copySeed(seed)}
                className="border border-gray-700 rounded px-1.5 py-0.5 text-[10px] hover:bg-gray-800 hover:text-gray-200"
                title="Copy seed to clipboard"
              >
                {copied ? 'copied' : 'copy'}
              </button>
              <button
                onClick={() => setParam('seed', seed)}
                className="border border-gray-700 rounded px-1.5 py-0.5 text-[10px] hover:bg-gray-800 hover:text-gray-200"
                title="Load this seed into the Seed field so the next generation reuses it"
              >
                use
              </button>
            </div>
            <div className="mb-1 text-gray-400">
              run {result.run_id} · open {(result.stats.open_fraction * 100).toFixed(0)}% · pools{' '}
              {result.stats.pool_cells} · rivers {result.stats.river_cells}
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-0.5">
              {Object.entries(result.stats)
                .filter(([k]) => k.endsWith('_s'))
                .map(([k, v]) => (
                  <span key={k}>
                    {k.replace('_s', '')}: {v}s
                  </span>
                ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
