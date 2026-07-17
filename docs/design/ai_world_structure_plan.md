# AI-Designed World Structure — Plan

*Status: agreed, not yet implemented. This document records the decisions made
with Filip (2026-07-17) and the milestone plan. The technical background is
`docs/systems/hierarchy.md`; the player-facing vision is
`docs/design/world_hierarchy_designer_guide.md` (parts of which this plan
supersedes — see "Docs to update").*

## The goal

The player enters a setting — just free text, e.g. "generate a fantasy world"
— and the world is designed entirely from that. An LLM decides what scale the
world should have (solar system, planet, region, city…), what structure that
implies, and which visual map generator draws each part, by reading the
generator registry's own descriptions and picking the best fit (falling back
to an abstract graph map when nothing fits). The structure is a tree of
scales decided per world, not a preset:

> Fantasy world → what is needed? A solar system: does any registered
> generator sound like a solar-system map? Pick it; if not, use abstract.
> Inside it, space stations (interior maps for small ones, city maps for
> large ones) and planets (planet map generation) — and so on iteratively
> down to the leaf level.

Templates stop being the source of structure and genre. They were originally
meant to define *scale* only, but grew into whole-world presets (genre,
prompts, vocabulary), which limits players to the shipped presets or forces
them to author their own. That role ends; the AI design pass takes it over.

## Decisions (agreed 2026-07-17)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Upfront vs lazy structure | **Upfront structure, lazy content** (for now): the AI designs the entire scale/type tree at world creation in one design document; map *content* is still generated lazily during play. |
| 2 | Where structure design lives | **`hierarchy_design`** (Claude's call, no preference stated): it runs after rules and lore, so the structure is designed knowing the world's name, history and factions. `world_form` keeps its current job (map style, step directives). |
| 3 | Root map generation | **Unify with expansion**: the root map becomes "the first expansion" — LLM authors the root's content where the scale calls for it (a ~10-node solar system), the chosen generator lays it out, recursion handles the rest. Procedural terrain overworlds keep their existing path *as one of the generators*, not as the privileged root case. |
| 4 | Templates | **Delete** the world-template system (UI, routes, package, JSON files). Old worlds must keep working (see back-compat). |
| 5 | Parallel maps | **Keep**, but the design-pass prompt must make clear they are for genuinely side-by-side same-scale planes (surface/underworld) and should not be used unless absolutely necessary — a solar system's planets are *children* of the root, not parallels. |
| 6 | Abstract fallback | **Existing** abstract mode (world_map without terrain), not a new dedicated generator. Revisit if solar-system-scale maps look wrong at ~10 nodes. |
| 7 | Player review | **Show the proposed design**: the AI-designed structure appears as a normal reviewable/editable step in the wizard before generation proceeds. |
| 8 | Sequencing | Claude's call — the milestone plan below, ordered so each milestone is independently shippable and testable. |

## Where the code is today

What already points in this direction:

- **`world_form`** (`worldgen/steps/world_form.py`) — an LLM design pass that
  reads the seed prompt and decides world kind, terrain-vs-abstract map
  style, skips, and per-step directives. The pattern to extend, not replace.
- **Generator registry** (`worldgen/generation/registry.py`) — labeled
  catalog with prose descriptions, built for selection by id;
  `list_generators()` is prompt-ready. `star_system`/`region` are reserved
  stubs that fail loudly.
- **Free-text levels** — hierarchy levels are "vocabulary, not code"; the
  compiled world carries `hierarchy.levels` and play-time expansion reads
  them (`enrichment/maps_expand.py`).
- **Lazy expansion** — `MapExpansionEngine` opens any named node into a child
  map (LLM authors content, generator lays it out, anchored connections,
  `MAX_DEPTH=6`).

The gaps:

- Levels come **only from the template** (`templates/__init__.py`,
  `facade.py` root-generator pick, `compile_world` hierarchy passthrough).
  `hierarchy_design` today only fills parallel maps + a pregenerate list
  *within* the template's fixed vocabulary.
- Child maps can **only be interiors**: `allowed_child_levels()` hard-filters
  to `generator_id == "interior"` — the promised "pipeline rework".
- The root map is **always the procedural pipeline** (`map_generation` step,
  30–500 nodes); an authored small-N root (solar system) doesn't fit.
- Templates also supply things that must survive their deletion: system
  framing voice, connection/sub-location vocabulary (`template_vocab`,
  consumed in `enrichment/context.py`, `sites.py`, `maps_expand.py`),
  default node density.

## Target architecture

One sentence: **`hierarchy_design` becomes the structure-design pass whose
output — an ordered list of AI-authored levels, each bound to a registered
generator — drives both root generation and recursive expansion, through one
unified "author content, lay out with the level's generator, recurse"
path.**

The redesigned `hierarchy_design` step outputs:

- `levels`: ordered top-to-bottom, each `{level_type, label, guidance,
  generator_id, nestable?}`. `guidance` is where per-location-type nuance
  lives ("stations open into interiors when small, city maps when large") —
  the expansion LLM already chooses the child's level at expansion time, so
  heterogeneous children fall out naturally.
- `generator_id` is chosen from the registry catalog, which is injected into
  the prompt (id + label + description, implemented generators only).
  Normalization clamps unknown/unimplemented ids to the abstract fallback
  (world_map with `map_style: abstract` semantics).
- `vocabulary`: what `template_vocab` used to carry — `site_sub_noun`,
  `connection_looks` — now authored per world by the same pass.
- `parallel_maps` (kept, discouraged in guidance) and `pregenerate` (kept).
- `notes`: the AI's reading of the structure, shown to the player (decision
  7 — the step is reviewable like any other, which the pipeline-driven
  wizard gives us for free).

Generation then means: build the root map via the unified path (authored
content when the root level's generator needs LLM content or the scale is
small; procedural terrain when the root level is a terrain overworld), then
expand the `pregenerate` list; everything else generates lazily during play
exactly as now, except children may use any implemented generator, not just
`interior`.

## Milestones

Ordered so each lands alone, is testable alone, and nothing template-shaped
is deleted before its replacement exists.

### M1 — `hierarchy_design` designs the structure

The AI authors levels + generators + vocabulary; everything downstream that
read `template.resolved_levels()` reads the step's data instead. Children
are still interiors-only; the root path is unchanged. Shippable and visible
on its own (the wizard shows the designed structure).

- Rewrite `worldgen/steps/hierarchy_design.py`: new schema (`levels`,
  `vocabulary`, `parallel_maps`, `pregenerate`, `notes`), guidance including
  the discourage-parallels wording, a custom `generate()` that injects the
  generator catalog (precedent: `world_form._catalog`), and a
  `normalize_hierarchy_design()` clamping generator ids, requiring ≥1 level,
  falling back to `DEFAULT_LEVELS` on junk output.
- Thread the levels: `facade.generate_step` root-generator pick and
  `facade.compile_world` read from the step's data (template only as
  fallback until M2); `compiler.py` compiles `hierarchy.levels` and the
  world's `vocabulary` (feeding the seams that read `template_vocab` today).
- Default node density: move `default_total_nodes` from template `map` into
  the step output (a `map_density` hint) or drop to the existing 100
  default — decide at implementation.
- Mock fixtures (`fixtures/mock_data.py: mock_hierarchy_design`) updated to
  the new shape so offline/mock mode exercises the full path.
- Tests: normalization (junk ids, empty levels), catalog injection, levels
  reaching the compiled world, expansion still working over AI-authored
  levels, old worlds (no new-shape data) unaffected.

### M2 — Delete templates

With structure, vocabulary and framing coming from the pipeline, remove the
template system.

- Delete `worldgen/templates/` (package + JSON), `test_templates.py`, the
  wizard's template picker (`ui/WorldBuilder/WorldBuilderWizard.jsx`),
  `template_id` in `routes.py` (`/api/world/templates`, `/api/world/pipeline`
  query param, generate request) and `frontend/src/lib/api.js`.
- Facade: remove `list_templates`/`get_template`/`template_for`/
  `apply_to_step` plumbing; `DEFAULT_SYSTEM_FRAMING` moves to a neutral home
  (steps/base or llm generation). `world_form`'s `world_kind` is appended to
  the framing so the genre voice is per-world, replacing per-template
  framings.
- Back-compat (old worlds must load and play):
  - `compiled["template_vocab"]` passthrough in `compiler.py` stays; old
    saved worlds already snapshot their vocabulary into world state.
  - Worlds whose state lacks `hierarchy_levels` but named a now-deleted
    template: on load, fall back to `DEFAULT_LEVELS` (world → interior) —
    same behavior the default template produced. A one-time bake of levels
    into stored state for known template ids is optional; decide at
    implementation whether any real worlds need more than the default pair.
  - `migrate.py DEFAULT_LEVELS` stays as the universal fallback.
- Tests: template-era worlds still load, compile, expand; API surface no
  longer advertises templates.

### M3 — Unified generation: root-as-first-expansion, non-interior children

The pipeline rework. One generation path: *author content for this map (LLM
or procedural), lay it out with the level's generator, connect to parent,
recurse into what the design marks upfront.*

- `MapExpansionEngine` grows per-generator content contracts: `interior`
  keeps today's authored-rooms flow; `world_map`/`city_roadnet` children get
  a child-scoped compiled context (premise + the anchor node's identity) and
  run their procedural build with an `id_prefix`, followed by per-map
  enrichment (labeling/descriptions) scoped to the new map. Terrain rasters
  for terrain-style child maps persist per map id (extends
  `persistence.terrain_dir` usage).
- `allowed_child_levels()`: drop the interior-only filter — offer every
  strictly-lower (or nestable-same) level whose generator is implemented.
- Root map: `map_generation` builds the root according to the designed root
  level. Terrain overworld root → existing procedural path, unchanged.
  Authored-content root (abstract solar system and similar) → the same
  expansion-style authored flow, with no parent to anchor to.
- Parallel maps: generalized off the legacy `world_map`-multilayer
  special case (`generation/maps.py` warning path) — each parallel plane is
  built by its own level generator and joined by authored connections.
- Audit the seams that assume "root = the world": enrichment scoping
  (`collect_scope_content`, landmark/faction `scope`), `start_locations`,
  RAG world-index entries per map (`map_world_entries` already exists),
  fog-of-war init, `MAX_DEPTH`.
- Tests: solar-system-style world end-to-end in mock mode (abstract root,
  planet child via world_map, station child via interior), city child
  expansion, parallel-plane world, terrain-root regression suite green.

### M4 — Polish and docs

- Update `docs/design/world_hierarchy_designer_guide.md` (template authoring
  section is obsolete; "level vocabulary is fixed at creation" now means
  "designed by the AI at creation"), `docs/systems/hierarchy.md`,
  `docs/systems/world-building.md`.
- Prompt tuning across scales (solar system vs. city vs. dungeon), density
  defaults per generator, wizard copy for the structure review step.
- Remove any leftover template references (`wbruntime/backfill.py`, module
  backends) found by a final grep.

## Deferred / out of scope (revisit later)

- A dedicated small-N abstract layout generator (decision 6 chose the
  existing abstract mode; revisit if solar systems render like sparse
  villages).
- Implementing `star_system` / `region` generators — the registry + AI
  selection means new generators start getting picked the moment they're
  registered, with no pipeline changes.
- Lazy *structure* (designing new levels mid-play) — decision 1 chose
  upfront structure for now.
- New parallel planes appearing mid-play (existing known limit, unchanged).
