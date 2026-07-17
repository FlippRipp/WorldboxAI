# DEPRECATED: The Terrain-Region System

Status: **unplugged, code kept**. The `terrain_regions` pipeline step is no
longer registered (`steps/__init__.py`); everything below remains in the tree
so the idea can be remade later. Deprecated alongside it: `layer_design` and
`layer_rules` (replaced by `hierarchy_design` + world_format 2 parallel
maps — see `docs/systems/hierarchy.md`).

## What it was

An LLM step (`wbworldgen/worldgen/steps/terrain_regions.py`, `after =
"terrain_generation"`) that authored the world's named regions as free text:

```jsonc
{"regions": [{"layer_id", "name", "terrain", "climate", "description"}]}
```

Regions were pure prose — no polygons or coordinates. Spatial territory was
derived later during map generation.

## The merge join (the keystone)

`compiler.merge_geography_steps` built `compiled["regions"]["regions"]` by
iterating **only** `terrain_regions` data, then attached
`natural_landmarks` and `society_factions` entries onto each region by
**name equality** (`region == region.name`, optionally scoped by
`layer_id`). Out of that join came `named_locations` — the authored
settlements and landmarks that map generation anchored onto real nodes.

Subtlety worth keeping: authored settlements were added with an **empty
description** on purpose — a non-empty description would be bound onto the
map node and make the `node_descriptions` enrichment step treat the node as
already described, permanently skipping its real flavor text.

Consequence of the join: if `terrain_regions` produced nothing, every
landmark/faction/settlement was silently dropped (they only reached the map
by attaching to a region name). This coupling is the main reason the system
was inflexible.

## Territory carving (two code paths, both still in-tree)

- **Terrain-aware** (`world_map.py:_generate_with_terrain` →
  `region_affinity.partition_regions`): each region's `terrain`+`climate`
  prose was parsed against keyword rules and the raster map was carved into
  contiguous territories *before* node placement; authored anchors were
  placed inside their region's territory; filler nodes inherited a region
  via `region_affinity.region_at`.
- **Abstract fallback** (`world_map.py:_assign_regions`): importance-weighted
  center nodes were picked (one per region), multi-source BFS expanded
  membership over the Delaunay graph, and the nested `_bind` closure walked
  each region's nodes stamping authored settlements (importance ≥ 8) and
  landmarks (≥ 6).

Both produced `WorldMap.regions` (`{region_name, node_ids, center_node_id}`)
and a `region` field on nodes.

## Downstream consumers it fed

- Enrichment context (`region` block: terrain/climate/factions/landmarks per node)
- `_build_location_context` region lines and the `player_location_region`
  mutation select
- `memory.py` region/landmark/faction embeddings (`source_type: "region"` etc.)
- Start-location candidate context (`_find_node_region`)
- MapRenderer's Voronoi region polygons + hover panel

All of these are guarded and degrade silently on empty regions — nothing
crashes; geography just disappears. Legacy worlds still compile through
`merge_geography_steps` (kept for the migration path), so old saves keep
their regions.

## What replaced it

Hierarchy **scopes**: landmarks/factions carry a single free-text `scope`
naming the map they belong to (empty = root). `compiler.collect_scope_content`
gathers them per scope, `attach_scope_content` puts them on each MapRecord,
and `world_map.bind_named_locations` stamps authored settlements/landmarks
onto nodes with the same importance floors the region `_bind` used. The
organizing role regions played (a mid-scale named area) is intended to
return as a first-class **map level** instead.

## Revival notes

The good idea — named mid-scale areas with terrain/climate character — maps
cleanly onto the hierarchy: implement the reserved `region` generator
(`generation/registry.py` stub) so a world map's area can open into a
region-scale child map. The carving machinery (`region_affinity.py`,
`_assign_regions`, `_enforce_region_contiguity`) is reusable for laying out
such a child map from prose descriptions; the old step's schema shows what
the authoring form looked like. Attach content by scope (the region map's
label), not by a name-join.
