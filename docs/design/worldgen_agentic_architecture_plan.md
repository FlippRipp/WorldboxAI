# Worldgen Architecture — Modularity & Agentic Builder Plan

*Status: Arc A landed (A1–A5, 2026-07-19; RuntimeHost still pending, rides
along with the next backend.py change). Arc B refined and DECIDED with Filip
(2026-07-19): unit+trigger pass model, legacy per-node endpoints removed in
B1, panel generalization as B1.5. B1 landed 2026-07-19 (d169491); B1.5
landed 2026-07-19 (301f3c1); B2 landed 2026-07-19 (22954e9) — next item B3.
Arc C refined into C1a/C1b; its four open questions are deliberately STILL
OPEN — settle them with Filip before C1a starts. Records the structural assessment of
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

## Design principles

The rules the arcs are built on. Every change to `wb_worldgen` — inside this
plan or after it — should be checkable against these; a change that needs to
break one should say so explicitly and update this section, not quietly
deviate. Cite them by number in reviews.

**Architecture:**

- **P1 — A capability is a catalog entry.** Every unit of build behavior
  (step, generator, pass) is a self-describing registry entry: id, label, a
  description that doubles as the planner's selection text, declared
  contracts. Unknown ids fail loudly, always. If a behavior can't be
  described as a catalog entry, it isn't a capability yet — don't bolt it on
  as a special case.
- **P2 — Dropped file, no dispatcher edits.** Adding a capability = adding a
  module that registers itself. If a new step/generator/pass requires editing
  a shared dispatcher, run loop, or if-ladder, the seam is broken — fix the
  seam, don't extend the ladder. (This is the convergence thesis made
  operational: what an engineer adds by file-drop, the planner discovers by
  catalog.)
- **P3 — Explicit contracts only.** Dependencies are named fields on small
  typed objects (`GenServices`); subsystems compose the facade's *public*
  API plus `services`. Nothing reaches into another object's privates. When
  new code needs something the contract lacks, extend the contract visibly
  (documented field/method — precedent: `append_map_node` on
  `enrichment_store`), never grep-couple.
- **P4 — Steps produce, `design.py` reads.** "What did the AI decide for
  this world" has exactly one query surface. Core never imports step
  internals; the import direction is steps → core, never back.
- **P5 — One execution path, many sources.** Classic wizard, seeded worlds,
  and planned (agentic) builds all drive the same orchestration
  (`generate_step`, the generators, the pass engine) — what varies is the
  source (live LLM vs `force_mock`, default plan vs authored plan). Never a
  second execution engine, never a duplicated walk. (Precedent: A5's
  `seed_world`.)
- **P6 — AI generates, user steers.** Every stage is editable, rerollable,
  approvable. Agent decisions are persisted artifacts the UI can show and
  the user can veto — never conversation state inside one long LLM call.
  Executed work is immutable; revisions may only add or narrow future work.
- **P7 — Loud validation, never silent repair.** A plan or request that
  references an unknown capability or an unsatisfied dependency is rejected
  before execution. Validation errors are surfaced, not patched around.

**Working rules:**

- **P8 — Refactors land alone.** Structural items land with zero behavior
  change, full suite green (module by path + root), pushed to `main` before
  the next item begins. Behavior changes ride their own commits with their
  own tests.
- **P9 — Structural budgets, not token caps.** Never cap tokens/characters
  when assembling LLM context (project rule). Budget plans in structural
  units: max items, max passes per scope, max revision rounds.
- **P10 — Boundaries are drawn once.** Shared types stay dependency-free
  (`mapmodel` is stdlib-only — keep it that way). When two roadmap items
  would redraw the same line, the later item owns it (precedent: A4 skipped
  the engine split because B1 draws that boundary).

## Where the structure stands

Assessment from the 2026-07-19 read-through (~22.7k lines non-test Python;
the pain points below are the *pre-Arc-A* record of why the refactor exists —
A1–A5 have since addressed #1–#4 and most of #5).

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
    mapmodel/               # A4 ✓ shared map types + join_key — stdlib-only, keep it so
    terrain/                # unchanged
    worldgen/
      base.py, steps/       # step catalog (unchanged mechanism)
      generation/           # generator catalog + overworld/city/interior builders + binding (A4 ✓)
      enrichment/           # B1: engine.py (scheduler) + passes/{label,describe,review}.py + context.py
      expansion/            # A4 ✓ maps_expand + sites (child-map/site generation)
      design.py             # A3 ✓ the one query surface over the AI's world design
      services.py           # A1 ✓ GenServices — the explicit engine contract
      compiled_cache.py     # A2 ✓ CompiledWorldCache
      prompts.py            # A5 ✓ world-prompt message builders
      compiler.py, persistence.py, facade.py (701 lines, delegates + wiring), ...
  wbruntime/                # unchanged internally; explicit RuntimeHost instead of _HOST (pending)
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

*Landed 2026-07-19: the engine split was deliberately skipped (P10 — B1
draws that boundary); `join_key` was promoted into `mapmodel` and the
compiler's identical `_norm_name` unified onto it; `enrichment/context.py`
stayed in place (see B1).*

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

*Landed 2026-07-19: facade 1131→701 lines. `seed_world` became async and a
`force_mock` flag threads through `generate_step`/`StepContext`/the root
expanders (seeding never spends tokens even with a live LLM wired); terrain
and enrichment steps stay `{}` in seeds for exact parity. The moved
start-location orchestration reads dependencies through the new public
`facade.services` property (P3). All remaining facade shims had live
callers and stayed.*

---

## Arc B — Self-describing capabilities

Arc B is where the two extension axes get their payoff, and it produces the
catalog the agentic planner will read.

### B1. Enrichment pass registry — size L

*Decided with Filip (2026-07-19): unit+trigger model; legacy per-node
endpoints removed; panel generalization split out as B1.5.*

Today the engine's run loop hardcodes the label/describe phases and review
is a bespoke method. The 2026-07-19 code review established that the
originally sketched per-node spec does not survive contact with review:
review works on **maps** (one critique call per map, then per-node repairs),
fires **mid-run** (as soon as a map's labeling completes, engine.py ~674),
and its repairs **re-invoke** the label and describe implementations.
The registry therefore models work units and triggers explicitly:

```python
@dataclass
class PassSpec:
    id: str                   # "label", "describe", "review", "history", ...
    label: str
    description: str          # doubles as planner-catalog text
    unit: str                 # "node" | "map" — what one work item is
    selector: Callable        # (compiled, unit, state) -> pending? (rework-aware)
    run: Callable             # async (services, unit, context) -> field updates
    after: list[str]          # ordering constraints (describe after label)
    triggers: dict | None     # {"on_map_complete": "label"} — interleaved firing
    batchable: bool           # may share one LLM call across units (label batching)
```

- **The engine keeps everything genuinely shared** — importance ordering,
  per-unit pending computation, concurrency/semaphore, batching for
  batchable specs, retries + the services-owned rate-limit backoff, cancel,
  SSE progress events, flush cadence, compiled-cache handling — and iterates
  *registered passes* instead of the phase tuple. One scheduler, two
  iteration shapes (node passes, map passes).
- **Layout:** `enrichment/passes/{label,describe,review}.py`. Prompts and
  post-processing move out of the engine into these modules as importable
  module-level functions, so review's repair path imports the label/describe
  implementations directly — pass-to-pass reuse is a plain import, not
  engine plumbing. The engine split deferred from A4 happens here (run
  orchestrator vs pass bodies), drawing the boundary once (P10).
  `enrichment/context.py` stays put: it is the node-context assembly the
  passes share; `expansion/` and `start_locations` importing it
  cross-package is accepted and documented.
- **Review as a first-class pass:** `unit="map"`,
  `triggers={"on_map_complete": "label"}` — preserving today's interleaved
  behavior exactly (a map is reviewed the moment its naming completes;
  best-effort, a review failure never fails the run). Standalone review runs
  via `enrich_run(phase="review")`. This is what makes review visible in the
  C1 planner catalog.
- **Removals (decided):** the legacy per-node endpoints
  `/enrich/label_next` + `/enrich/describe_next`, the facade delegates
  `enrich_next_label`/`enrich_next_description`/`review_enrichment_labels`,
  and the engine methods `label_next`/`describe_next`. They have no frontend
  callers (the UI drives the SSE `enrich/run` API, which already covers
  `rework`), and they duplicate the selection semantics the specs now own.
  Tests migrate to `enrich_run(count=1)` / `enrich_run(phase="review")`.

**Compatibility:** the `phase=` API on `enrich_run` maps onto pass ids
(`"all"` = every registered pass in dependency order). The SSE event shape
is unchanged for the built-ins (`type: phase|node|failed|done`, with the
`phase` field carrying the pass id). `rework` becomes a selector argument.
Existing settings (`world.enrichment_concurrency`,
`world.enrichment_batch_size`, `world.upfront_detail`) keep their meaning.

**Verify:** the existing `test_enrichment_run.py` suite passes with at most
call-site updates for the removed endpoints; new unit tests cover pass
registration + unknown-id failure (P1), node-vs-map scheduling, trigger
firing, batching only for batchable specs, and an event-stream compatibility
assertion (a built-ins run emits the same event sequence as before B1).

*Landed 2026-07-19 (d169491), with three recorded refinements against the
sketch above. (1) The per-pass ``selector`` callable became two predicates
— ``is_done``/``in_domain`` — because a monolithic selector contradicted
this section's own "the engine keeps per-unit pending computation" bullet:
with the predicates, rework/scoping/importance-floor/progress arithmetic
lives once in the engine and was verified to reproduce the old
``_pending_for_phase`` branch-for-branch. (2) Trigger timing preserved
*as-coded*, not as previously summarized: review fires when the label
phase completes, over every map that phase finished — the old code never
reviewed mid-phase. Triggered map passes emit no phase event (as before);
explicitly-requested map phases (``phase="review"``, a new invocation
shape) do. (3) The ``/enrich/review`` route (not in the decided removals)
survives with a byte-identical response, running the review pass through
``enrich_run(phase="review")``; review skipping (fewer than two named
nodes, failed reviewer call) returns zero-valued contributions so run
summaries keep their pre-B1 shape. Tests now fake LLM calls by
monkeypatching the pass-module functions (``label.generate_label`` etc.)
instead of engine attributes. Known pre-existing quirk, deliberately
preserved (P8): review repairs update the compiled nodes but not the
run's ``all_nodes`` copies, so a ``phase="all"`` run can describe a
relabeled node under its pre-review name — flagged for a separate
behavior-fix discussion.*

### B1.5 Enrichment panel over the pass catalog — size S (frontend)

`EnrichmentPanel.jsx` hardcodes exactly two phases
(`isLabeling ? 'label' : 'describe'`); a third registered pass would stream
events the panel miscounts. Directly after B1: a small endpoint serves the
pass slice of the catalog (id, label, description, unit), and the panel
renders one progress row per registered pass instead of the hardcoded
branches — "everything" runs all passes, each pass gets its run affordance,
and a future `history` pass appears without frontend edits (P2 extended to
the UI). Verify in the real browser (drive real Chrome — see project
memory; the Preview pane is unreliable for this UI).

*Landed 2026-07-19 (301f3c1). `/enrich/progress` was reworked in the same
stroke: per-pass done/total/per-map numbers computed from the registry
predicates, bucketed by map id first so they agree with the run's SSE
events (the old route bucketed by legacy layer id — the panel merges both
sources). Map passes render as run-affordance rows (review's fixes now
stream into the results list and rename nodes on the live map — the old
panel dropped `review_fix` events). No step↔pass mapping exists anywhere:
default selection is simply the first pass with pending work. Verified in
real Chrome (CDP) against a seeded world on a second dev stack
(`WB_PORT`/`WB_BACKEND`).*

### B2. One capability catalog — size S

Give the three registries a uniform `describe()` and one function that
renders the combined catalog (steps + generators + passes) as the document
an LLM — or a human — reads, in both structured (JSON) and human-readable
(markdown) forms. `hierarchy_design` already consumes the generator slice
(`list_generators()` is already describe-shaped); B2 makes the full catalog
a first-class artifact that C1's planner reads whole. Module-contributed
hooks (`HOOK_NAMES`) are explicitly *out* for now — that is Arc C open
question 4.

*Landed 2026-07-19 (22954e9): `describe_steps` / `describe_generators` /
`describe_passes` on their registries (every entry: kind, id, label,
description, declared contracts), aggregated by `worldgen/catalog.py` —
`capability_catalog()` (complete from a cold start via the
`register_default_steps` lazy-import idiom) + `render_catalog_markdown()`.
The B1.5 `/enrich/passes` route now serves the shared pass slice.*

### B3. Declared data dependencies — size M

Steps today declare ordering (`after`); they don't declare *data*. Add
optional `requires`/`produces` artifact declarations (e.g. `world_rules`
produces `rules`; `map_generation` requires `hierarchy`, produces `maps`).
`PassSpec` gets `requires` too (e.g. every pass requires `maps`; `describe`
requires `labels`) so plan validation covers pass items, while ordering
*within* enrichment stays the registry's `after`.

Two guard rails from the review:

- **Pin the order first.** The current `resolve_order` resolves ties in
  declaration order; the derived topological order must reproduce today's
  default pipeline byte-for-byte, because chain-context order feeds prompts
  — a silent reorder changes generations subtly. Land a regression test
  asserting the exact current order *before* switching the derivation.
- **Validation is the executor's, not the sorter's.** The dependency checker
  is a standalone function evaluated against the *effective* item list
  (after `dynamic_skips`) — C1's executor calls it (P7). `resolve_order`
  itself keeps its current behavior and API.

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

### C1a. Build-plan step + plan executor (server) — size L

*Blocked on the open questions below — do not start until they are settled
with Filip.*

Insert a `build_plan` step after `world_form` (which already decides
per-world skips and map style — a proto-plan). Its generation reads the B2
catalog and the world idea and authors a plan artifact:

```jsonc
{
  "items": [
    {"id": "it_01", "capability": "step:lore", "config": {...},
     "note": "creation myth as corporate founding",
     "origin": "planned", "status": "pending"},
    {"id": "it_02", "capability": "step:hierarchy_design",
     "origin": "planned", "status": "pending"},
    {"id": "it_03", "capability": "step:map_generation",
     "origin": "planned", "status": "pending"},
    {"id": "it_04", "capability": "pass:label", "scope": {"layer": "root"},
     "origin": "planned", "status": "pending"},
    {"id": "it_05", "capability": "pass:history", "scope": {"importance_min": 3},
     "origin": "planned", "status": "pending"},
    {"id": "it_06", "capability": "pass:review",
     "origin": "planned", "status": "pending"}
  ],
  "notes": "why this shape"
}
```

- **Per-item execution state lives on the artifact.** Step items still
  persist their outputs into `world_state["steps"]` exactly as today; pass
  items have no step data, so their completion record is the item's
  `status` (`pending | running | done | failed | skipped`). That is what
  makes a planned build resumable mid-flight (P6), and `origin`
  (`default | planned | revision`) is the attribution trail C2 needs.
- **The executor validates, then reuses.** Validation = catalog membership
  (P1/P7) + the B3 dependency check against the effective item list + scope
  shape. It runs at authoring *and again at execution* — the module set may
  have changed between the two (a plan referencing a capability that
  disappeared fails loudly, never silently skips). Execution drives the
  *same* orchestration that exists today — `generate_step` for step items,
  generator builds, the pass engine with the item's scope mapped onto
  selector arguments — no second execution engine (P5).
- **The default plan — produced without an LLM call — is exactly today's
  pipeline** (`ordered_ids_for` + dynamic skips rendered as plan items with
  `origin: "default"`), so classic mode and agentic mode are one code path
  with two plan sources, and old worlds replay unchanged.
- `seed_world` becomes "execute the default plan with `force_mock`",
  closing the loop A5 opened (its pipeline-driving rewrite and the
  `force_mock` thread through `generate_step` are the ready seam).

**Verify:** `test_build_plan.py` — default-plan parity (a default plan
executes byte-identical to today's wizard flow in mock mode), validation
rejections (unknown capability, unmet dependency, bad scope), resume from a
half-executed plan, seed-world-as-plan parity. Plus a visual check of a
planned world's map output before pushing.

### C1b. Plan editor UI — size M–L

The wizard already renders per-world step lists (`ordered_ids_for` +
dynamic skips → `effectiveIds`/`skippedIds` in `WorldBuilderWizard.jsx`),
but two things are genuinely new, which is why the UI is its own item
rather than a C1a footnote:

- **Editing the plan artifact.** Free-text capability ids in the generic
  schema form would be unvalidated typing; the editor is catalog-driven —
  pick a capability from the B2 catalog, get its config/scope form, reorder,
  per-item reroll/veto/approve (P6). Until C1b lands, the plan artifact
  renders read-only through the existing step-data view.
- **Mixed progression.** A plan interleaves step items (today's form-per-step
  flow) with pass items (progress-bar work, EnrichmentPanel-style rows from
  B1.5). How the wizard's current-step/approval loop presents that mix is
  real UX design — sequence it after C1a proves the artifact shape.

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

Guardrails — decided in principle (P6/P7/P9), with the addition from the
review that they are **enforced by the executor, not trusted to the
prompt**:

- **Budgets in structural units** (P9): max items, max passes per scope,
  max revision rounds — plus the existing concurrency settings.
- **Convergence, enforced:** the executor rejects a revision that touches an
  executed item or exceeds the round budget — monotonicity is validation,
  not instruction-following.
- **Attribution:** `origin: "revision"` on everything a revision adds
  (mirrors the `origin` field on ConnectionRecords).

### Open questions — STILL OPEN (settle with Filip before C1a)

*Reviewed 2026-07-19 and deliberately left open rather than adopted; each
has a working proposal, but the proposals are inputs to that discussion, not
decisions.*

1. **Planner freedom.** Does `build_plan` choose only *which* capabilities
   and their configs/scopes (proposal), or can it also author new pass
   prompts ad hoc? Proposal: catalog-only at first; "ad-hoc pass authored
   from a prompt" can later be one registered capability
   (`pass:custom_prompt`) rather than a hole in the validation story.
   *Blocks: the plan schema (whether items may carry prompt text) and the
   validator's strictness.*
2. **Granularity of scopes.** Per-map, per-layer, per-importance-band
   selectors for passes (proposal); per-node plans would explode the
   artifact. *Blocks: the `scope` field shape and its mapping onto pass
   selectors.*
3. **Where interleaving lives.** Enrichment runs both inside the pipeline
   (upfront detail) and lazily at play time (backfill, expansion). Does the
   plan govern only build time (proposal for C1), or eventually also
   play-time policy ("this world backfills history lazily")? *Blocks:
   whether the artifact carries a play-time section C1a must not invent
   ad hoc later.*
4. **Module capabilities.** Should other modules (`wb_core_rpg`, ...) be
   able to register passes/steps into the catalogs directly, superseding
   some of the bespoke `HOOK_NAMES`? Powerful, but changes the module
   contract — separate discussion. *Blocks: B2's hook visibility and the
   catalog's namespace rules.*

---

## Sequencing

| Order | Item | Size | Status | Serves |
|-------|------|------|--------|--------|
| 1 | A1 GenServices (+A2 cache, folded in) | M | ✓ landed (13e9613) | everything downstream |
| 2 | A3 design.py | M | ✓ landed (f4d5251) | new map types |
| 3 | A4 package moves | M | ✓ landed (d0ca8c3, 839376a) | readability; staged B1 |
| 4 | A5 facade slimming | M | ✓ landed (9cf5ad4) | hygiene; staged C1 |
| 5 | B1 pass registry (+ engine split, legacy endpoint removal) | L | ✓ landed (d169491) | new LLM passes |
| 6 | B1.5 panel over the pass catalog | S | ✓ landed (301f3c1) | UI keeps the P2 promise |
| 7 | B2 catalog | S | ✓ landed (22954e9) | agentic substrate |
| 8 | B3 dependencies (+ order-pin test first) | M | next | plan validation |
| 9 | C1a build-plan step + executor (server) | L | ⛔ open questions first | agentic mode v1 |
| 10 | C1b plan editor UI | M–L | after C1a | steering the plan |
| 11 | C2 reactive loop | L | exploratory | agentic mode v2 |

(A5 landed before B1 — the reverse of the original ordering; nothing
depended on the order.) RuntimeHost (`backend.py` half of A1) can ride along
with any item that touches `backend.py`. Every item lands per P8: module
suite by path + root suite green (`git checkout -- test_data` after), pushed
to `main` before the next begins. Arc B changes no behavior (its
verification is the existing per-feature test files plus new unit tests for
the extracted contracts — pass-spec registration, event-stream compat,
dependency checking); the one deliberate exception is B1's decided removal
of the dead legacy endpoints. C1a adds behavior and gets `test_build_plan.py`
plus a visual check of a planned world's map output before pushing.
