# Worldgen Architecture — Modularity & Agentic Builder Plan

*Status: proposed (2026-07-19). Records the structural assessment of
`modules/wb_worldgen` and the phased plan discussed with Filip. Near-term
extension axes: new map generators and new LLM passes. Long-term goal: an
agentic builder — an LLM receives a world idea and figures out what it needs
to build. Companion docs: `docs/design/ai_world_structure_plan.md` (the
AI-designed structure work this generalizes), `docs/systems/hierarchy.md`
(data contract), `docs/systems/world-building.md` (pipeline design).*

## The goal

Two things at once, and they turn out to be the same thing.

**Near term** we want two kinds of extension to be cheap:

1. **New map types** — a `star_system` generator, a `region` generator, a
   dungeon generator… Today this is *almost* registry-only
   (`generation/registry.py`), which is the model to protect.
2. **New LLM passes** — a history pass, a cultures pass, per-region flavor,
   extra critique passes. Today a new *pipeline step* is easy (drop a module
   in `steps/`), but a new *node pass* over map content is not: label,
   describe and review are hardcoded into `EnrichmentEngine.run`'s phase loop
   (`phase in ("label", "describe", "all")`).

**Long term** the builder should be agentic: the LLM reads the world idea and
decides what needs to exist — which artifacts, which passes, which maps —
instead of walking a fixed step list.

The convergence thesis: **hand-extensibility and agent-discoverability are
the same property.** A capability an engineer can add by dropping a file is a
capability a planning LLM can select by reading a catalog — *provided* the
capability is self-describing (id, description, input schema, output
contract) and uniformly invocable. The refactor that makes generators and
passes pluggable is not a detour on the way to the agentic builder; it is
most of the road.

The system already contains the seed of this pattern, twice:

- `hierarchy_design` reads the generator registry's own descriptions and
  binds each designed level to a generator — an LLM choosing tools from a
  catalog. (This was the explicit goal of
  `ai_world_structure_plan.md`: "does any registered generator sound like a
  solar-system map? Pick it; if not, use abstract.")
- `enrich review_labels` critiques generated content and revises it — a
  critic/repair loop.

The agentic builder is these two patterns generalized from "the map ladder"
to "the whole build". None of the phases below change player-visible
capabilities; Arc C adds a new mode without removing the current one.

## Where the structure stands

Assessment from the 2026-07-19 read-through (~22.7k lines non-test Python).

**Seams that already work — protect them:**

- Step registry (`worldgen/base.py` + `steps/`): self-registering, no
  dispatcher edits.
- Map-generator registry (`generation/registry.py`): labeled catalog, loud
  failure on unknown/unimplemented ids, descriptions double as the LLM's
  selection catalog. Expansion picks behavior off the spec's
  `needs_llm_content` contract, not off generator ids.
- Pure `compiler.py`; isolated `persistence.py`; `HookRegistry` for
  cross-module hooks; the self-contained `terrain/` package with its own
  params/pipeline and a documented promotion path (`terrain/step.py`);
  `wbruntime/` split into focused play-time modules.

**Pain points, ranked:**

1. **Implicit `host` god-objects, twice.** `EnrichmentEngine`,
   `SiteExpansionEngine` and `MapExpansionEngine` are built with
   `host=self` (the `WorldBuilder` facade) and reach into its *private*
   attributes (`_get_prompt` ×28, `_save_node_enrichment`,
   `_world_builder_temperature`, `_json_retry_attempts`,
   `_enrichment_semaphore`, `_llm_service`, `_flush_enrichment_cache`,
   `load_world`); the expansion engines additionally reach *through* the
   facade into the enrichment engine (`host._enrichment`) for its
   compiled-world cache. `wbruntime` functions take `backend.py`'s `_HOST`
   view (live module globals) and read four names: `_services`, `_backfill`,
   `world_builder`, `_site_tasks`. The contracts are discoverable only by
   grep; facade and engines are mutually dependent.
2. **The facade is three things**: composition root, legacy delegation shim
   (~30 one-liners), and a home for real logic with nowhere better to live
   (`seed_world`'s hand-rolled pipeline walk, ~250 lines of start-location
   pick/descend/author, the world-prompt message builders, the
   `_authored_root_level` / `_abstract_root_level` / `_root_generator_for`
   design queries). 1120 lines for "a thin coordinator".
3. **Core imports step internals.** `facade.py` imports `world_form` +
   `hierarchy_design` helpers; `compiler.py` and `generation/maps.py` import
   `designed_levels`; `maps_expand.py` imports the *private*
   `_build_layer_terrain` from the terrain step. "What did the AI design for
   this world" is queried from five-plus places, each reaching into step
   modules.
4. **Misplaced weight.** `wbworldgen/world_map.py` (1556 lines, the biggest
   file) fuses the shared map data model (`MapNode`/`WorldMap`, used by
   everything), the overworld generation algorithms, and
   `bind_named_locations`. `maps_expand.py` (child-map *generation*, by its
   own docstring) lives in `enrichment/` because it borrows enrichment's
   host plumbing — symptom of #1.
5. **Smaller frictions.** The compiled-world cache lives inside
   `EnrichmentEngine` but invalidation is everyone else's job (facade save
   paths, `routes.py` directly). `backend.py` carries ~40 one-line
   `_HOST`-threading wrappers. `seed_world` duplicates the real path's
   three-way root-map branching in mock form. Legacy shims (`PipelineStep`,
   facade attribute aliases onto persistence internals) linger.

## Target shape

```
wb_worldgen/
  backend.py                # module adapter: wiring + engine-discovered hooks only
  routes.py, terrain_routes.py
  wbworldgen/
    mapmodel/               # A4: shared map types (MapNode, WorldMap, ...) — dependency-free
    terrain/                # unchanged
    worldgen/
      base.py, steps/       # step catalog (unchanged mechanism)
      generation/           # generator catalog + overworld/city/interior builders
      enrichment/           # node-pass catalog: label/describe/review as registered passes (B1)
      expansion/            # A4: maps_expand + sites move here from enrichment/
      design.py             # A3: the one query surface over the AI's world design
      services.py           # A1: GenServices — the explicit engine contract
      compiled_cache.py     # A2: CompiledWorldCache
      prompts.py            # A5: world-prompt message builders
      compiler.py, persistence.py, facade.py (thin), ...
  wbruntime/                # unchanged internally; explicit RuntimeHost instead of _HOST
```

Three catalogs — **steps** (pipeline stages), **generators** (map builders),
**passes** (node-level LLM work) — each self-describing, each with loud
failure on unknown ids. Arc C's planner reads all three.

---

## Arc A — Explicit contracts (refactor, zero behavior change)

The goal of Arc A is that every dependency is a named field on a small typed
object, and every piece of logic lives in the package whose name describes
it. Public surfaces (routes, hook names, test entry points) stay stable
throughout; each item lands independently with the suite green.

### A1. `GenServices` and `RuntimeHost` — size M

Replace `host=self` with an explicit dataclass built by the facade:

```python
@dataclass
class GenServices:
    llm: ...                    # LLM service (live reference)
    prompts: ...                # get_prompt(prompt_id, fallback, **kwargs)
    enrichment_store: ...       # save_node_enrichment / flush — persistence-backed
    compiled: CompiledWorldCache  # see A2
    load_world: Callable
    temperature: float | None
    json_retry_attempts: int
    semaphore: asyncio.Semaphore
```

The three engines take `services` and stop importing/knowing the facade.
Tests build one fake `GenServices` instead of monkeypatching facade privates.
Mirror move in `backend.py`: a `RuntimeHost` dataclass with the four fields
`wbruntime` actually reads (`services`, `world_builder`, `backfill`,
`site_tasks`); the `_HostView` live-globals trick disappears or shrinks to a
test shim. *(RuntimeHost is the cheapest slice and matters least for the
stated extension axes — do it while `backend.py` is open, not first.)*

**Verify:** full suite; the enrichment/expansion/site tests already cover the
engines' behavior.

### A2. `CompiledWorldCache` — size S

Extract `_load_compiled` / `invalidate_compiled` / `release_terrain` /
`_update_cached_node` from `EnrichmentEngine` into a small class owned by the
facade and handed to engines via `GenServices.compiled`. Save paths
invalidate through it explicitly. Kills the `host._enrichment` reach-through
from the expansion engines and makes the invalidation contract visible.

### A3. `design.py` — one query surface over the AI's world design — size M

Move the scattered "what did the AI decide for this world" reads behind one
module: `designed_levels`, `world_kind`, `dynamic_skips`,
`root_generator_for`, `authored_root_level`, `abstract_root_level`, the
map_style→generator alignment helpers. Steps keep *producing* the data;
`design.py` owns *reading* it. Core (`facade`, `compiler`,
`generation/maps`, `maps_expand`) stops importing from `steps/` — including
the private `_build_layer_terrain` import, which becomes a properly exported
function.

This is the highest-leverage item for the **new map types** axis: a new
generator or level type touches the registry plus, at most, one alignment
table in `design.py` — not five call sites.

### A4. Package geometry — size M, mechanical

- Split `world_map.py` → `mapmodel/` (dataclasses + `compass_direction`;
  dependency-free), `generation/overworld.py` (`WorldMapGenerator`),
  `generation/binding.py` (`bind_named_locations`).
- Move `enrichment/maps_expand.py` + `enrichment/sites.py` →
  `expansion/`. After A1 they no longer need enrichment's plumbing, so the
  move is `git mv` plus imports.
- Optionally split `enrichment/engine.py` (1204 lines) into the run
  orchestrator (concurrency, batching, cancel, backoff) and the LLM call
  implementations — this also pre-stages B1.

### A5. Slim the facade — size M

- World-prompt message builders (`build_world_prompt_messages`,
  `build_world_questions_messages`, `build_world_prompt_fold_messages`,
  `scenario_*`) → `prompts.py`; facade re-exports for compatibility.
- Start-location bodies (`llm_pick_start_location`,
  `_descend_start_location`, `author_location`, `_persist_generated_start`)
  → `start_locations.py`, which already holds the pure half.
- `seed_world` drives the normal pipeline with the mock strategy forced
  instead of re-walking it — removes the duplicated authored/abstract/
  procedural root branching.
- Drop delegation one-liners whose only callers were the engines (now served
  by `GenServices`); keep the public API used by routes/tests byte-stable.

---

## Arc B — Self-describing capabilities

Arc B is where the two extension axes get their payoff, and it produces the
catalog the agentic planner will read.

### B1. Enrichment pass registry — size L

Today the engine's run loop hardcodes the label/describe phases and review
is a bespoke method. Introduce a pass registry in the mold of
`generation/registry.py`:

```python
@dataclass
class NodePassSpec:
    id: str                   # "label", "describe", "review", "history", ...
    label: str
    description: str          # doubles as planner-catalog text
    selector: Callable        # (compiled, node, state) -> needs this pass?
    run: Callable             # async (services, node, context) -> field updates
    after: list[str]          # pass ordering constraints (describe after label)
    batchable: bool           # can share one LLM call across nodes
```

The engine keeps everything that is genuinely shared — importance ordering,
concurrency/semaphore, batching, retries/backoff, rate-limit handling,
cancel, SSE progress events — and iterates *registered passes* instead of a
phase tuple. `label`, `describe`, `review` become the three built-in specs;
their prompts and post-processing move out of the engine into their spec
modules. A new pass (history, cultures, per-region flavor, extra critique)
is then a dropped file, exactly like a step or a generator.

**Compatibility:** the `phase=` API on `enrich_run` maps onto pass ids
(`"all"` = every registered pass in order). Existing settings
(`world.enrichment_concurrency`, `world.enrichment_batch_size`,
`world.upfront_detail`) keep their meaning.

### B2. One capability catalog — size S

Give the three registries a uniform `describe()` and one function that
renders the combined catalog (steps + generators + passes) as the document
an LLM — or a human — reads. `hierarchy_design` already consumes the
generator slice of this; B2 just makes the full catalog a first-class
artifact. Module-contributed hooks (`HOOK_NAMES`) should eventually appear
here too, so other modules' capabilities are visible to the planner.

### B3. Declared data dependencies — size M

Steps today declare ordering (`after`); they don't declare *data*. Add
optional `requires`/`produces` artifact declarations (e.g. `world_rules`
produces `rules`; `map_generation` requires `hierarchy`, produces `maps`).
Ordering is derived topologically with the current declaration order as the
tiebreak, so behavior is unchanged — but a planner (Arc C) can now *check*
a plan ("this pass needs maps; nothing in the plan produces maps") instead
of trusting the LLM, and a human gets the dependency graph for free.

---

## Arc C — The agentic builder

### The framing decision

An agentic builder must not sacrifice the three properties the current
system gets right:

- **User steering.** The philosophy is "AI generates, user steers" — every
  stage editable, rerollable, approvable. An opaque tool-calling loop breaks
  this.
- **Resumability.** Worlds persist per-step (`world_state["steps"]`), drafts
  resume mid-build. Whatever the agent decides must be a persisted artifact,
  not conversation state inside one long LLM call.
- **Loud validation.** `get_generator` fails loudly on unknown ids; the
  planner gets the same treatment — a plan referencing an unknown capability
  or an unsatisfied dependency is rejected before execution, never silently
  patched.

The proposal that satisfies all three: **the plan is itself a step.**

### C1. Build-plan step + plan executor — size L

Insert a `build_plan` step after `world_form` (which already decides
per-world skips and map style — a proto-plan). Its generation reads the B2
catalog and the world idea and authors a plan artifact:

```jsonc
{
  "items": [
    {"capability": "step:lore", "config": {...}, "note": "creation myth as corporate founding"},
    {"capability": "step:hierarchy_design"},
    {"capability": "step:map_generation"},
    {"capability": "pass:label", "scope": {"layer": "root"}},
    {"capability": "pass:history", "scope": {"importance_min": 3}},
    {"capability": "pass:review"}
  ],
  "notes": "why this shape"
}
```

- The plan renders in the existing pipeline UI (the frontend already handles
  per-world step lists via `ordered_ids_for` + dynamic skips — this extends
  that pattern rather than inventing a new one). The user can edit, reorder,
  reroll, approve — the plan is a step like any other.
- The executor validates against the catalog + B3 dependencies, then runs
  items through the *same* orchestration paths that exist today
  (`generate_step`, generator builds, the pass engine). No second execution
  engine.
- The default plan — produced without an LLM call — is exactly today's
  pipeline, so classic mode and agentic mode are one code path with two plan
  sources, and old worlds replay unchanged.
- `seed_world` becomes "execute the default plan with mocks", closing the
  loop on A5.

### C2. Reactive loop — size L, exploratory

C1 is plan-then-execute. The genuinely agentic version lets the builder look
at what it made and revise: after `map_generation`, notice the world is an
archipelago and add a `pass:naval_routes` item; after `review`, decide a
region needs a dedicated culture pass. Concretely: an optional
`review_plan` capability that runs between items, receives compact summaries
of produced artifacts (the `context_view` trimming mechanism already exists
on steps for exactly this), and may append/modify *not-yet-executed* items —
edits to the plan artifact, persisted, visible and vetoable in the UI, with
executed items immutable.

Guardrails to decide up front:

- **Budgets in structural units, not tokens.** Project rule: no token caps
  in context assembly. Budget the *plan* instead — max items, max passes per
  node class, max revision rounds — plus the existing concurrency settings.
- **Convergence:** revision rounds are bounded and monotone (a revision may
  add or narrow work, not reopen executed items).
- **Attribution:** plan items record `origin: "default" | "planned" |
  "revision"` so we can see what the agent chose to do (mirrors the
  `origin` field on ConnectionRecords).

### Open questions (to discuss before C1 starts)

1. **Planner freedom.** Does `build_plan` choose only *which* capabilities
   and their configs/scopes (proposed), or can it also author new pass
   prompts ad hoc? Proposal: catalog-only at first; "ad-hoc pass authored
   from a prompt" can later be one registered capability
   (`pass:custom_prompt`) rather than a hole in the validation story.
2. **Granularity of scopes.** Per-map, per-layer, per-importance-band
   selectors for passes probably suffice; per-node plans would explode the
   artifact.
3. **Where interleaving lives.** Enrichment currently runs both inside the
   pipeline (upfront detail) and lazily at play time (backfill, expansion).
   Does the plan govern only build time (proposal for C1), or eventually
   also play-time policy ("this world backfills history lazily")?
4. **Module capabilities.** Should other modules (`wb_core_rpg`, ...) be
   able to register passes/steps into the catalogs directly, superseding
   some of the bespoke `HOOK_NAMES`? Powerful, but changes the module
   contract — separate discussion.

---

## Sequencing

| Order | Item | Size | Serves |
|-------|------|------|--------|
| 1 | A1 GenServices (+A2 cache, folded in) | M | everything downstream |
| 2 | A3 design.py | M | new map types |
| 3 | A4 package moves | M | readability; stages B1 |
| 4 | B1 pass registry | L | new LLM passes |
| 5 | A5 facade slimming | M | hygiene; stages C1 |
| 6 | B2 catalog + B3 dependencies | S+M | agentic substrate |
| 7 | C1 build-plan step | L | agentic mode v1 |
| 8 | C2 reactive loop | L | agentic mode v2 (exploratory) |

RuntimeHost (`backend.py` half of A1) can ride along with any item that
touches `backend.py`. Every item lands independently: suite green
(`python -m pytest`, then `git checkout -- test_data`), pushed to `main`
before the next begins. Arcs A and B change no behavior, so their
verification is the existing per-feature test files plus new unit tests for
the extracted contracts (`GenServices` fake, pass-spec registration,
plan validation). C1 adds behavior and gets its own test file
(`test_build_plan.py`) plus a visual check of a planned world's map output
before pushing.
