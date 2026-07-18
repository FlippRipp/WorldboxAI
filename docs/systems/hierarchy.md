# World Hierarchy — Technical Contract (world_format 2)

The designer-facing counterpart is `docs/design/world_hierarchy_designer_guide.md`.
This file is the engineering contract: the data shapes, the invariants, and
what future edits touch.

## Data shapes

A compiled world dict (`world_data`) carries:

```jsonc
{
  "world_format": 2,
  "root_map_id": "root",
  "hierarchy": {"levels": [{"level_type", "label", "generator_id", "guidance", "nestable"?, "terrain"?}], "notes": "..."},
  "maps": { "<map_id>": MapRecord },
  "connections": [ ConnectionRecord ],
  "rules": {...}, "lore": {...}, "regions": {...(legacy only)}, ...
}
```

**MapRecord**

```jsonc
{
  "map_id": "root" | "m_<8hex>" | "site_<node>" (migrated) | "<legacy layer id>" (migrated),
  "label": "The Broken Keep",
  "level_type": "interior",          // FREE TEXT from hierarchy.levels
  "description": "...",
  "parent_map_id": null,             // null ONLY on the root map
  "anchor_node_id": null,            // null = parallel sibling (underworld); set = child of that node
  "generator_id": "world_map" | "city_roadnet" | "interior",
  "nodes": [...], "edges": [...],    // same per-map shapes as ever
  "regions": [...], "roads": [...],  // optional geometry extras
  "config": { "map_width", "map_height", "instant_travel"?, ...,
              "terrain"? },          // terrain marker: child map has its own
                                     // rasters under terrain/<map_id>/
  "legacy_layer_id": "surface",      // migrated worlds only; terrain raster URLs key on it
  "landmarks": [...], "factions": [...],  // scope-attached authored content
  "rules": [...],                    // migrated per-layer rules
  "schema": 2
}
```

**ConnectionRecord**

```jsonc
{
  "id": "c_<8hex>",
  "from": {"map_id", "node_id"},
  "to":   {"map_id", "node_id"},
  "kind": "door" | "shuttle" | ...,  // FREE TEXT, AI-facing flavor
  "name": "...", "description": "...",
  "travel": {"mode": "instant"} | {"mode": "journey", "turns": N},  // the ONLY field the engine interprets
  "bidirectional": true,
  "requirements": "",                // free text, enforced NARRATIVELY by the storyteller
  "hidden": false,                   // secrets: in AI context marked SECRET, excluded from player_passage
  "origin": "generated" | "improvised" | "migrated"
}
```

## Invariants

- **Node ids are globally unique** across all maps (child maps prefix
  `<map_id>:n<k>`; migrated ids are kept verbatim). This keeps
  `revealed_node_ids`, backfill futures and RAG `source_id`s flat.
- **Reachability is defined ONLY by connections** — never by tree position.
  Every generated child map must ship ≥1 entrance connection anchoring it to
  its parent (`MapExpansionEngine._build_map` raises otherwise).
- **The engine interprets only `travel.mode` and the endpoints.** `kind`,
  `requirements` and level semantics are text owned by the AI.
- **Edges never cross maps**; Dijkstra/journeys are per-map, transitions
  between maps always go through a connection (or a custom transition).
- The player is always `(player_location_map_id, player_location_node_id)`;
  the breadcrumb is derived by walking `parent_map_id`.
- Everything generated at play time is write-once cached under
  `data/worlds/<id>/maps/` and synced session → save → RAG through
  `wbruntime/sync.py`.

## Key modules

| Concern | Where |
|---|---|
| Accessors (maps, connections, breadcrumb, children) | `wbworldgen/worldgen/mapspace.py` (+ re-exported in `wbruntime/worldspace.py`) |
| Migration (legacy map/map_layers/sites → v2) | `wbworldgen/worldgen/migrate.py` |
| Generator registry | `wbworldgen/worldgen/generation/registry.py` |
| Interior layout (deterministic) | `wbworldgen/worldgen/generation/interior_layout.py` |
| Child-map expansion (one LLM call) | `wbworldgen/worldgen/enrichment/maps_expand.py` |
| Movement/passages/transits/improvised ways | `wbruntime/travel.py` |
| Mutation schema (Reader-facing fields) | `wbruntime/schema.py` |
| Location/intro context + primer | `wbruntime/context.py` |
| Background backfill + expansion triggers | `wbruntime/backfill.py`, `wbruntime/expansion.py` |
| Session/save/RAG sync | `wbruntime/sync.py` |
| Pipeline step for structure | `wbworldgen/worldgen/steps/hierarchy_design.py` |
| World design (AI-shaped pipeline: map_style, skips, per-step directives) | `wbworldgen/worldgen/steps/world_form.py` |
| Scope-attached landmarks/factions | `compiler.collect_scope_content` / `attach_scope_content` |
| Frontend normalizer | `modules/wb_worldgen/ui/lib/mapspace.jsx` |

## What future edits touch

- **New map generator** (e.g. a real `star_system` orbit layout): one file
  registering a `MapGeneratorSpec` in `generation/registry.py` — that's all.
  The registry's descriptions are the selection catalog the structure-design
  LLM reads, so a newly registered generator starts getting picked for
  fitting levels immediately; implemented generators are automatically
  offered for child expansion too.
- **New genre/scale**: nothing — the `world_form` step reads the seed prompt
  and shapes the pipeline per world (terrain vs abstract vs city map,
  optional steps, per-step coverage directives), and `hierarchy_design`
  authors the level ladder, generator bindings, terrain flags and vocabulary
  for that world.
- **New level type**: AI-authored text — it appears when a world's structure
  design calls for it (or the player edits the structure step).
- **New connection kind**: data only — write it into a connection record or
  the world's `connection_looks` vocabulary (authored by `hierarchy_design`;
  template-era worlds keep their snapshot).
- **New travel mode**: one branch in `wbruntime/travel.py`
  (`_connection_turns` / `begin_transit`) plus the schema description.
- **New movement trigger**: a handler block in `wbruntime/travel.py`'s
  `on_mutate_state` + a field in `wbruntime/schema.py`.
- **Mid-play creation of new parallel maps** (deferred): additive — author a
  MapRecord + one connection through the same sync path expansion uses.

## Reader-facing mutation fields (summary)

- `player_location_node_id` — same-map move (30-option cap, current map only)
- `player_passage` — connection id, or `enter:<node_id>` (child map created
  on demand, await-bounded)
- `custom_transition` + `custom_transition_target` +
  `custom_transition_becomes` (`one_time`/`open_passage`/`conditional_passage`)
  + `custom_transition_new_location` — improvised ways and teleports
- `discover_passage` — unhide a secret connection the fiction earned
- `travel_interrupted` — pause a journey
