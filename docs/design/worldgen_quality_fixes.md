# Worldgen Quality Fixes

Why generated worlds could come out "uninspired" — locations that ignore the
premise, no trace of the designed structure — and the fixes that landed.
Diagnosed on the real "Lustra System" world (2026-07-19): a solar-system
premise whose build steps produced a genuinely spacey design (planets Cinder,
Verdantia, gas giant Mirage, moons, stations, a "system graph of celestial
bodies" hierarchy) that the map layer then flattened beyond recognition.

## Problem

Three independent failures compounded:

1. **Concurrent steps broke their own data contract.** One-shot generation
   ran `society_factions` and `natural_landmarks` concurrently (a leftover
   optimization from when both only depended on `terrain_regions`). But
   factions' `region` field must reference the **areas** natural_landmarks
   authors — running blind, the factions call invented its own region names
   ("Fleshport", "Halo Ring", "GeneForge Jungles") while the areas came out
   as "The Tattered Belt", "The Glimmering Core", "Verdantia's Wilds".

2. **Exact-match joins scattered everything.** The compiler joined `region`
   references by exact normalized string equality; even "Neon Docks" vs
   "The Neon Docks" failed on the article. In Lustra, **zero** of seven
   factions joined any region, so every faction place fell through to
   "most important free node anywhere on the map": the Station Confederacy's
   council chambers landed in Verdantia's feral jungles, the Fleshport slave
   market's Auction Blocks in the shiny corporate Core — 24 of 30 faction
   places outside their group's home area. Cross-step name dedup was also
   exact, so "CyberSleaze Spire" (faction settlement) and "The CyberSleaze
   Spire" (landmark inside the Halo Ring) both survived as two places in two
   different regions.

3. **Abstract worlds got a terrain-shaped map.** For `map_style: "abstract"`
   the root map was still the procedural Poisson-disc scatter: 50 anonymous
   nodes typed settlement/crossroads/wilderness, Delaunay edges, authored
   names stamped onto the most important ones. Neither the hierarchy_design
   root guidance ("nodes are celestial bodies... Cinder, Verdantia, Mirage")
   nor the world_form `map_generation` directive was consumed by anything —
   only LLM steps receive directives, and map generation had no LLM. So no
   planet ever became a node (planets survived only as region-overlay
   names), venue-scale faction places ("Diplomat's Lounge") sat as siblings
   of moons on a solar-system map, and parallel planes (the Datasphere)
   burned half the node budget on 49 unnamed fillers. The root layer's
   authored `level_type` ("system_graph") was also dropped for a hardcoded
   `"world"` on the parallel-maps path.

## Landed

### F1 — One-shot mode runs steps strictly sequentially

`_run_one_shot_generation` (modules/wb_worldgen/routes.py) no longer gathers
`society_factions` with `natural_landmarks`; the declared `after` dependency
is honored so factions always see the authored areas in chain context. The
society_factions guidance now tells the model to copy an authored area name
EXACTLY (and points at Notable Features, not the deprecated Terrain &
Regions step). Regression test:
`test_one_shot_society_factions_sees_natural_landmarks_data`.

### F2 — Tolerant, two-level region/name joins

`_norm_name` (worldgen/compiler.py) and the new `_join_key`
(wbworldgen/world_map.py) are case-, whitespace- and leading-article-
tolerant, applied to every authored-name join: region joins, cross-step name
dedup, `part_of` anchor resolution, and region-preference binding.

`_region_resolver` (worldgen/compiler.py) resolves a `region` reference in
two levels:

1. it names an **area** (tolerantly) → that area;
2. it names an authored **landmark** ("based in Fleshport") → the area that
   landmark sits in, **and** the entry's places anchor `part_of`/adjacent to
   the landmark itself, so a cartel "based in Fleshport" clusters around
   Fleshport instead of merely somewhere in the same belt.

Anything else resolves to "" — explicitly unplaced beats placed-at-random.
Applied in both compiler paths (`merge_geography_steps` for the legacy
region merge, `collect_scope_content` for world_format 2 scope content;
the latter also accepts legacy `terrain_regions` names as area vocabulary).
Every faction of the real Lustra world resolves correctly under these two
rules. Tests: the "Tolerant region references" section of
test_location_anchoring.py.

### F3 — Abstract worlds get an authored root graph

New module `wbworldgen/worldgen/generation/abstract_graph.py` plus
`expand_abstract_root` in `worldgen/enrichment/maps_expand.py`, routed from
the facade when the world design declared `map_style: "abstract"` and the
root level is a non-terrain `world_map` (`_abstract_root_level`). Terrain
and city worlds, and worlds predating the design step, keep the procedural
path unchanged.

One full-attention LLM call per layer authors the map's real structure at
its own scale — in a solar system: the star, planets, moons, stations —
finally consuming the hierarchy root guidance and the world design's
`map_generation` directive. The call receives the authored areas and every
named location, and must place each one either as a node or inside a node's
`contains` (venue-scale places live INSIDE the place they belong to, fixing
the scale collapse). Parallel planes are authored the same way from their
own scoped content, with crossing nodes paired into interlayer connections.

The pure half (`abstract_graph.py`) is deterministic and junk-tolerant:

- `normalize_abstract_graph`: server-side ids, article-tolerant dedup,
  regions resolved against the areas, `contains` folded into
  `contained_locations` (descriptions recovered from the authored list),
  and a safety net folding every unplaced authored place into its anchor /
  region hub / most important node — authored content is never dropped.
- `ensure_crossing_nodes`: authored crossings first, synthesized shortfall.
- `layout_abstract_graph`: region clusters ringed around the map center,
  golden-spiral placement inside each, relaxation to minimum spacing,
  connectivity repair.
- `mock_abstract_parsed`: offline stand-in so mock worlds and tests run the
  identical pipeline; a live layer whose LLM output normalizes to nothing
  degrades to it (an abstract world never falls back to the scatter).

Nodes keep the engine `type` vocabulary (settlement/landmark/waypoint by
importance — start-location preference and map styling still work) and
carry the world's own noun in a new `kind` field ("planet", "station",
"gate"). The root layer's authored `level_type` now survives on both the
authored path and the procedural parallel-maps path (worldgen/generation/
maps.py). Tests: test_abstract_root.py.

## Result on the Lustra data

Regenerating Lustra's root map through the new pipeline (offline author,
real step data): 24 nodes instead of 50 — all named or crossing points, all
in their resolved home region; Fleshport contains the Bazaar of Spent
Dreams, Flesh Markets, Chain's Den, Auction Blocks, Slave Docks and Velvet
Exchange; the Halo Ring contains the Confederacy seat and CyberSleaze
Spire; the article-variant spire duplicate is gone. The live path
additionally authors the celestial bodies themselves (Cinder, Verdantia,
Mirage) from the hierarchy guidance, which the offline author cannot
invent.

## Still open (from the same review)

- Enrichment label/description prompts pass region text but not the
  hierarchy level guidance, and speak the terrain type vocabulary
  ("crossroads") on non-terrain maps; the new `kind` field is not yet
  surfaced to them.
- The LLM call log's `full_input` field was empty for every call in the
  reviewed export — only 200-char summaries were logged; worth checking the
  inspector logging path.
