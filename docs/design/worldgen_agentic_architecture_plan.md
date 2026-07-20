# Worldgen Architecture — Modularity & Agentic Builder Plan

*Status: Arcs A and B COMPLETE (A1–A5, B1, B1.5, B2, B3 — all landed
2026-07-19; RuntimeHost still pending, rides along with the next backend.py
change), and Arc B verified end-to-end on live LLMs the same day (see "Live
verification of Arc B" — three follow-up discussion items surfaced). Arc C
REDESIGNED 2026-07-19 with Filip: the plan-artifact approach (old C1a/C1b/C2)
is superseded by a tool-calling agent loop plus a conversational ideation
phase — settled choices are recorded as D1–D5 in Arc C, the old four open
questions are resolved or dissolved there (module capabilities survives as
the one open question, deferred, non-blocking). C1 (toolbox registry +
tools + lints) LANDED 2026-07-19 (e989488); C2 (agent harness + evaluator)
LANDED 2026-07-19 (275d8b9); C3 (build observer UI) LANDED 2026-07-19
(f039a87) — the recorded live run is deliberately open: Filip drives it
through the C3 observer (a first live smoke ran 3 healthy turns before
being stopped on request; see C2's landed note). C4 (ideation) LANDED
2026-07-19 (b73c275) — Arc C code-complete; with it, the CLASSIC ENTRY IS
DISABLED (decided with Filip at C4 start; see C4's landed note) — agent
mode is the one way to build a new world, "for now". Outstanding: Filip's
live test (build + conversation check). v2a (structural surgery toolset)
DESIGNED and LANDED 2026-07-19 (51e8c0d) on Filip's go, ahead of the
original evidence gate — see the v2a section in Arc C; the live test now
exercises the 18-tool catalog. C5 (ideation notes + note verifier +
review gate) DESIGNED and LANDED 2026-07-19 with Filip (36a32db…903bca4,
five commits) — scoped brief notes (N1–N3), a read-only note-verifier
agent the builder can argue with (N4–N6), and an end-of-build review
gate whose veto relaunches a fix run (N7); the catalog is now 19 tools
and the outstanding live test covers notes end to end. C6 (world
explorer + classic-system removal) LANDED 2026-07-19: the post-creation
list view is replaced by a map-first explorer over the compiled world,
the classic sequential wizard/review system is deleted UI+routes deep,
and in-progress worlds recover through agent-build adoption — see the
C6 section. The Ecstasy Veil live build (2026-07-19, Filip's second
live run) exposed four silent-contract defects — all fixed same day
(5174f95, 39380ca; see "Live-run findings — the Ecstasy Veil build") —
and settled the design of v2c (checkpoint/revert, the agent's undo),
LANDED 2026-07-19 (60889d7) on Filip's go: every mutating action is
auto-checkpointed and a build-scoped revert tool restores byte-exact.
v2d (the expand_node tool) LANDED 2026-07-19 (e7d2b33) on Filip's go,
design delegated: child maps — the wall that felled both live builds —
are now reachable through the same expansion surface play-time uses.
C7 (one conversation: user
messages into the running build + ideation merged as the chat phase of
one agent session) DESIGNED 2026-07-20 with Filip — decisions U1–U6
settled (two agents, one session; brief edits as description-gated
tools; no authority machinery on user input; transcript on demand via a
read tool), two forks open, staged C7a (mid-build channel) then C7b
(merged front door). C7a LANDED 2026-07-20: the user can speak into the
running build (queue → turn-boundary observations, never-dropped unread
recording), the agent answers via ``say`` chat bubbles, user words
become contract through update_prompt/update_rules/update_notes (the
no_compromise veto lock held conservatively while fork 1 is open), and
read_conversation serves the transcript on demand — the catalog is now
25 tools. C7b LANDED 2026-07-20 — Arc C's front door is one two-phase
agent session: the first message lazily creates a draft world and opens
a message-paced chat phase (a second agent on a restricted brief
catalog), Go flips the same session/artifact/stream into the build,
the chat phase is resumable from its artifact across restarts, the
drafts are server truth with a hand-edit surface, and the stateless
``/ideation-turn`` machinery is deleted. Fork 2 is resolved
(lazy-create + explicit discard, no sweeps); fork 1 stays open, the
veto lock still held conservatively. The enabling fix rode along:
brief writes are now metadata-surgical (``update_brief``) — a C7a-era
``save_world`` side effect silently flipped drafts to finished on
every brief edit. See the C7 section.
Records the structural assessment of
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
*(Amended 2026-07-19 at C4: Filip decided the classic ENTRY goes away with
the ideation front door — one path to maintain, "for now". The classic
machinery — server generation routes, step-review flow for pre-existing
drafts, post-build editing — all remains; only the pre-start screen's
Generate World/skip-review affordances were removed. P5 is untouched:
every mode still drives the same orchestration.)*

## Design principles

The rules the arcs are built on. Every change to `wb_worldgen` — inside this
plan or after it — should be checkable against these; a change that needs to
break one should say so explicitly and update this section, not quietly
deviate. Cite them by number in reviews.

**Architecture:**

- **P1 — A capability is a catalog entry.** Every unit of build behavior
  (step, generator, pass — and, with Arc C, agent tool) is a
  self-describing registry entry: id, label, a
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
  and agent-driven builds all drive the same orchestration
  (`generate_step`, the generators, the pass engine) — what varies is the
  source (live LLM vs `force_mock`, fixed default pipeline vs agent-chosen
  actions). Never a second execution engine, never a duplicated walk.
  (Precedent: A5's `seed_world`.)
- **P6 — AI generates, user steers — at the boundaries the mode defines.**
  *(Rescoped 2026-07-19 with the Arc C redesign.)* Classic mode: every
  stage editable, rerollable, approvable, as today. Agent mode: steering
  concentrates in the ideation conversation (co-authored brief + world
  rules), the explicit go-ahead, and unrestricted post-build editing;
  during the build the user is deliberately out of the loop — steering is
  replaced by *observability* (persisted todo/action-log artifacts
  streamed live, cancel always available), not approval gates. *(Rescoped
  again 2026-07-20 with C7: the user gains a conversational channel into
  the running build — messages land as plain observations at turn
  boundaries, and the brief-edit tools let user words amend the contract.
  An advisory voice, not approval: no gates return; the agent still
  decides and acts.)* In both
  modes, AI decisions are persisted artifacts the UI can show — never
  conversation state inside one long LLM call.
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
  units: max items, max passes per scope, max revision rounds, max agent
  turns / tool calls / fix rounds per build.
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
      agent/                # C1–C2: toolbox registry (the 4th catalog) + lints + harness + evaluator
      design.py             # A3 ✓ the one query surface over the AI's world design
      services.py           # A1 ✓ GenServices — the explicit engine contract
      compiled_cache.py     # A2 ✓ CompiledWorldCache
      prompts.py            # A5 ✓ world-prompt message builders
      compiler.py, persistence.py, facade.py (701 lines, delegates + wiring), ...
  wbruntime/                # unchanged internally; explicit RuntimeHost instead of _HOST (pending)
```

Four catalogs — **steps** (pipeline stages), **generators** (map builders),
**passes** (node-level LLM work), and, with C1, **tools** (the agent's
action surface, wrapping the other three plus reads and targeted writes) —
each self-describing, each with loud failure on unknown ids. Arc C's agent
reads them all, rendered into its system prompt.

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
catalog the agentic builder will read.

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
a first-class artifact that Arc C's agent reads whole (rendered into its
system prompt). Module-contributed
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
  (after `dynamic_skips`) — Arc C's tool layer calls it (P7; in agent mode
  the "list" is what the world has produced so far plus the requested
  action — a per-action precondition check). `resolve_order` itself keeps
  its current behavior and API.

*Landed 2026-07-19 (0b0cab0): `requires`/`produces` on `Step` and
`PassSpec` (both, not requires-only — the plan's own "describe requires
labels" example needs pass produces to validate), declared for every
built-in; `catalog.py::check_data_dependencies(items, steps=None)` walks
an ordered `{"kind", "id"}` item list, order-aware, loud on unknown ids;
the order-pin test plus an all-8-skip-combinations test proving every
legitimate effective pipeline validates clean. Requires are hard needs
only (landmarks does not require terrain); `map_generation` requires
`hierarchy` per the example here — its procedural fallback serves
old-world replay, which never enters the checker. The catalog markdown
now annotates requires/produces for the planner.*

### Live verification of Arc B (2026-07-19)

One full end-to-end run on live LLMs (OpenRouter; `deepseek-v4-pro` as
reader/storyteller, `deepseek-v4-flash` as the fast slot), driven through
the real surfaces: one-shot pipeline via the API, enrichment through the
EnrichmentPanel in real Chrome, standalone review via its panel Run
affordance. World: **The Shattered Sea** (`the_shattered_sea`, kept in
`data/worlds` as the reference specimen) — the model chose `terrain`
style, skipped nothing, and designed two 50-node maps (surface archipelago
+ "The Drowned Deeps" parallel realm); 100 nodes, 60 majors, 22 pre-named
by authored-location binding. Pipeline 398s; enrichment ~3.5 min.

**Verified live:**

- Registry-driven scheduling honors the importance floor: exactly the 60
  majors labeled + described (the other 40 left to lazy play-time detail);
  ~54 enrichment LLM calls with zero transient retries, zero rate-limit
  backoffs, zero failed nodes.
- Batched labeling: ~38 missing names in a handful of batched fast-slot
  calls (~2 min); **zero duplicate names across both maps** — used-name
  threading and batch dedup hold up against a real model.
- SSE + panel: the phase event flips rows to scoped totals (22/60 →
  60/60), per-map bars track both maps live (root 35/35, deeps 25/25),
  the map renames in real time from node events, and REVIEW rows render
  fixes with old name + reviewer objection.
- Descriptions: 48 of 60 carry resolved `${link_id|Name (direction)}`
  references, zero bare link tokens, clean UTF-8 on disk.
- Review as a standalone map pass: flagged a real implied-containment pair
  the label batch had invented on the Deeps ("Siren's Bell" /
  "Siren's Bell-Tower", non-adjacent), relabeled both with the objection
  as steering ("God-Fall Scar", "Spire of Salt and Bone") and reworked
  both descriptions to match; all persisted.

**Follow-ups surfaced (discussion items, deliberately not scheduled):**

1. **One-shot worlds have no enrichment-panel host.** `skip_review` marks
   a world complete without enrichment step entries, and both the wizard's
   complete-state and `WorldReviewScreen` render enrichment UI only for
   steps with data — so a one-shot world offers no upfront-enrichment
   affordance after saving (it details lazily during play). Pre-existing,
   not an Arc B regression; the live run bridged it by writing a
   `node_labeling` entry via the save-step API. Options: always render the
   enrichment step on the review screen, or have one-shot completion
   commit empty enrichment entries the way `seed_world` does.
2. **The review trigger never fires in the default lazy-detail flow.**
   `on_map_complete` requires *every* node on a map named, and
   floor-limited runs never complete a map — so automatic review only
   happens with `world.upfront_detail = full` (or when play-time backfill
   happens to finish a map). Faithful to the pre-B1 code, but it means the
   interleaved review is effectively dormant in the default configuration;
   worth deciding whether the trigger should also fire on
   "floor-scope complete", or whether standalone review after the upfront
   run should be part of the default flow.
3. **Review repairs vs the run's node copies** (already recorded in B1's
   landed note): repairs update compiled nodes but not the run's
   `all_nodes` copies, so a `phase="all"` run can describe a relabeled
   node under its pre-review name.

*(Arc C's agent mode subsumes #1 and #2 for agent-built worlds — the agent
schedules enrichment and review explicitly and the done-gate checks the
result. Classic-mode worlds still carry both.)*

---

## Arc C — The agentic builder

*Redesigned 2026-07-19 with Filip, superseding the plan-artifact design
(old C1a/C1b/C2 — see "Superseded" at the end of this arc). No code from
the old design existed; the pivot cost one rewrite of this section.*

### The shape

Building a world has two phases with one explicit gate between them:

1. **Ideation (C4).** The user and the AI converge on what the world *is*,
   conversationally — a natural back-and-forth that is part of the flow,
   replacing today's optional, button-initiated `WorldPromptInterview`
   rounds. The first work item is co-authoring a few **world rules** that
   define the world; they double as the build's evaluation rubric (D3/D4).
   The AI judges when the idea feels settled and *offers* the go prompt;
   the user's go-ahead is the approval moment.
2. **The build (C1–C3).** After the go-ahead the user is out of the loop.
   A server-side agent works the way a coding agent does: it keeps a todo
   list, calls tools, verifies its own output against the rules, fixes
   what verification finds, and repeats until the done-gate passes. The
   user watches — the todo list and action log stream live — and can
   cancel; they do not approve steps. *(C7, designed 2026-07-20, softens
   the out-of-the-loop stance: the user may speak into the running build
   — see the C7 section.)*

The three properties the original framing protected, re-resolved:

- **User steering** moves to the boundaries: the co-authored brief, the
  explicit go-ahead, and unrestricted post-build editing through every
  existing surface. Mid-build approval gates are deliberately gone;
  observability replaces them (P6, rescoped accordingly).
- **Resumability**: the loop runs in the backend, which survives Android
  killing the PWA (Termux keeps the process alive — only the frontend
  dies). The client *reattaches* to a running build exactly as the
  one-shot path already does (`_generating` disk metadata).
  Backend-restart resume is a recorded v2 nicety, not a v1 requirement.
- **Loud validation** does more work than ever: every tool validates its
  arguments against its registry entry and the engines' invariants, and a
  rejected action returns to the agent as an observation it must react to.
  The error-feedback loop is the mechanism, not an exception path (P7).

The convergence thesis survives intact — the B2 catalog and B3 dependency
data stop being a document a planner reads once and become the agent's
toolbox: rendered into its system prompt, callable as tools, driving the
same orchestration as the wizard (P5). Nothing from Arcs A–B is discarded.

### Decisions (settled with Filip, 2026-07-19)

**D1 — Tool surface v1: the agent's write surface = the user's existing
write surface + the capability catalog.**

- *Read everything:* compiled world, step data, `design.py` queries, the
  capability catalog, the world rules, the lint report.
- *Catalog capabilities, full parameter surface:* run a step (config +
  steering note), run a pass (scope, count, rework, plus a new **guidance
  channel** — generalizing the objection-steering the review repair path
  already threads into label/describe). Steered rework is the agent's
  primary fix instrument: most evaluator findings are content findings,
  and steered regeneration is the invariant-safe repair for LLM content.
- *One ad-hoc capability:* `pass:custom` — agent-authored prompt + scope
  + namespaced output slot. Bespoke content goes through a registered,
  budgeted, validated capability, not around it.
- *User-parity writes:* `edit_node` (name/description through the
  enrichment store's existing write path, enforcing name dedup and
  link-token validation) and step-data patches (through the save-step
  surface the wizard already uses). Parity with what the app already
  trusts a human to do — no new invariant exposure.
- *Deliberately withheld in v1:* structural surgery — add/remove nodes,
  connection rewiring, terrain edits. No existing surface offers it and
  it carries the heaviest invariants (terrain layers, hierarchy
  consistency, compiled-cache coherence). The v1 recourse is regenerating
  the owning step with a steering note; structural tools are the
  designed-not-improvised v2 extension if evaluation shows a recurring
  wall.
- *Extensibility requirement (Filip):* the toolbox is itself a
  self-describing registry — the fourth catalog, P1/P2 applied to tools.
  A v2 tool is a file drop, not a harness edit.

**D2 — Loop mechanics: JSON action loop, not native tool-calling.**
`LLMService` has zero tool-call plumbing on any provider path — every LLM
interaction in the app is a JSON-structured completion — and the slot
rule (modules never name models) means the loop must work with whatever
`fastest/balanced/smartest` resolve to. One agent turn = one structured
completion: system prompt (brief + rules + toolbox catalog) + todo state
+ recent observations in; `{"tool": ..., "args": ...}` or a done claim
out. This inherits the existing hardening stack — fallback JSON parsing,
retries, the inspector, and the mock layer, which makes the harness
testable without tokens (canned action sequences). Accepted costs: no
parallel tool calls, and schema enforcement is the harness's job
(validate loudly, feed the rejection back as the next observation). The
smartest slot drives the loop (~tens of turns per build); the engines
keep the bulk token work on the fast slot exactly as today, so agent-mode
cost stays dominated by the same work the pipeline already does.

**D3 — Verification: deterministic lints + a rules-based evaluator, with
an end gate.** The lint report is a pure function over the compiled world
(duplicate names, orphan nodes, unresolved link tokens, connectivity) —
cheap ground truth. The child evaluator v1 is a single structured
critique call — world rules + lint report + content excerpts in, findings
out — not a tool-looping sub-agent (that is the v2 upgrade if it proves
too shallow). `evaluate(scope)` is a tool the agent may invoke at any
time; the harness enforces the gate: a build cannot be declared done
until a final evaluation runs clean or the agent explicitly accepts the
remaining findings with a recorded note.

**D4 — Ideation handoff: the brief.** Ideation distills into a persisted
world-brief artifact — the enriched prompt plus the co-authored world
rules and key constraints. The agreed rules feed the existing
`world_rules` step as *input* (the step expands them; downstream
consumers keep their contract; the co-authored core stays visibly
primary). The brief is the agent's standing instructions, re-read every
turn.

**D5 — Budgets are harness-enforced (P9).** Max agent turns per build,
max tool calls, max fix rounds per finding — structural units, never
trusted to the prompt. Cancel is always available.

### The old open questions — resolved or dissolved

1. **Planner freedom** → resolved by D1: catalog + `pass:custom` +
   user-parity writes.
2. **Granularity of scopes** → dissolved into tool arguments: pass scopes
   stay per-map / per-layer / per-importance-band, plus explicit node-id
   lists for rework. No plan artifact exists to explode.
3. **Where interleaving lives** → the agent governs build time only;
   play-time lazy detail is unchanged. A world-level play-time policy
   remains future work, and nothing here blocks it.
4. **Module capabilities** → the one that survives, now phrased "which
   tools does the agent get": modules registering steps/passes would
   extend the toolbox automatically. Changes the module contract —
   separate discussion, does not block Arc C.

### C1. Toolbox registry + tools + lints (server) — size M–L

The fourth catalog: a `ToolSpec` registry (id, label, description that
doubles as prompt text, parameter schema, invoke) with `describe_tools()`
joining the B2 catalog render. Every v1 tool wraps an existing surface —
`generate_step`, `enrich_run`, the enrichment store, save-step,
`design.py`, the compiled cache — no new orchestration (P5). Includes the
two small engine extensions: the rework guidance channel (D1) and the
lint report (D3). B3's dependency data becomes per-action precondition
checks: a step tool call is validated against what the world has produced
so far, loudly (P7). Unknown tool, bad args, unmet requires — all
rejected with errors shaped for the agent to read.

**Verify:** unit tests per tool (validation rejections + happy path on a
seeded world), lint fixtures with known defects, catalog-render test.

*Landed 2026-07-19 (e989488): ``worldgen/agent/`` — ``registry.py``
(ToolSpec + typed argument validation + ``invoke_tool`` raising
agent-readable ``ToolError``), ``lints.py``, ``tools/{read,build,edit}.py``
(11 tools), with three recorded refinements against the sketch. (1) The
guidance channel rides ``RunState.guidance`` → each pass's per-call
``context["guidance"]`` instead of new kwargs on ``generate_label``/
``generate_description`` — B1 made those functions the test patch points,
and a signature change would have broken every existing patcher; threaded
through single, batched, describe, review-critique and repair paths.
(2) `pass:custom` runs as an *ephemeral* ``PassSpec`` through the same
engine via new ``enrich_run(spec=)``/``engine.run(specs=)`` rather than a
globally registered pass — a global entry would render a bogus B1.5 panel
row and has no meaningful world-level ``is_done``; output lands namespaced
(``custom_<slot>``) so core fields cannot be clobbered by construction.
(3) ``patch_step`` excludes ``map_generation`` and the engine-driven
enrichment steps: a raw map patch is structural surgery through a side
door, which D1 explicitly withholds — map content goes through
``edit_node``/``run_pass``/regeneration. Preconditions are B3's
executor-side check via new ``catalog.produced_artifacts()`` (step
artifacts from non-empty step data, pass artifacts from node predicates)
diffed against ``requires``. The lint's map-isolation check is reachability
from the root over connections + parent anchors (a local
"has connections?" test would flag the root map itself). ``run_step`` pins
``_draft_id`` so terrain rasters land in the world's directory. Facade
additions: ``steps_by_id()``, ``enrich_run(guidance=, spec=)``. The B2
catalog renders the tools as its fourth section (per-argument lines
included — that render is the C2 system prompt's toolbox text).*

### C2. Agent harness + evaluator (server) — size L

The loop itself: brief + toolbox + todo in, actions out, budgets around
it (D5). The todo list is a persisted per-world artifact, updated by the
agent through todo tools and streamed over SSE with the same event
discipline as enrichment runs; the evaluator and done-gate (D3); cancel;
client reattach via the `_generating` metadata pattern. Launch
affordance: a "let the AI build it" action from the existing prompt box —
agent mode exists before ideation does, in the same slot `skip_review`'s
one-shot occupies today.

**Verify:** mock-driven harness tests — canned action sequences covering
budget exhaustion, invalid-action recovery, done-gate refusal, todo
round-trip, reattach — plus one recorded live run (Arc B's verification
pattern).

*Landed 2026-07-19 (275d8b9), with one decided refinement (settled with
Filip at C2 start): the todo list rides each turn's completion
(``{"thought", "todo", "action"|"done"}`` — one protocol shape, the todo
can never drift from the action stream) instead of separate todo tools.
``agent/harness.py`` owns the loop: per-turn system prompt = brief +
scenario + current world rules re-read from disk (D4) + the full catalog
render + protocol + budget status; ``agent_turn`` (smartest slot,
``json_retry_completion``) is the mock seam the 19 canned-sequence tests
patch. Budgets are settings (``world.agent_max_turns/_max_tool_calls/
_fix_rounds``, defaults 40/60/3); protocol errors, ToolErrors and LLM
failures return as next-turn observations (3 consecutive LLM failures
abort). The done-gate additionally refuses structurally empty builds (no
world_rules / no named nodes — an empty world lints clean, so the gate
itself must check); blocking findings must be fixed, accepted by key with
a recorded note, or auto-accepted after the fix-round budget.
``agent/evaluator.py``: lints + one smartest-slot critique (rules as
rubric, structural excerpts), stable finding keys
(``source:kind:map:node``), lint-only degradation offline — which is what
makes the gate testable without tokens; registered as the ``evaluate``
tool, whose tool-run results feed the same fix-round tracking. Events:
persisted ``agent_build.json`` artifact (indexed log, the SSE replay
cursor) + transient enrichment progress threaded through new
``ToolContext.on_event``; routes ``POST agent/build``, ``GET
agent/status``, ``POST agent/events`` (replay+live, race-safe dedupe,
artifact-served after restart), ``POST agent/cancel``. Launch affordance
lives beside skip-review in the wizard prompt box. Facade/persistence
additions: ``world_dir()``, snapshot ``last_event``; adopting an existing
world into a build forces ``complete=False`` (else draft_complete makes
the draft read finished). The recorded live run: a first smoke on
OpenRouter deepseek ran 3 healthy turns (11-item todo in pipeline order,
world_form → world_rules → lore, zero protocol/tool errors, cancel
honored at the turn boundary) before Filip stopped it; the full recorded
run is his C3-observer live test.*

### C3. Build observer UI — size M

The watching surface: live todo list, current action, streamed action
log, evaluator findings, cancel, reattach-on-relaunch. Builds on the
B1.5/EnrichmentPanel event patterns. Verify in real Chrome (project
memory: the Preview pane is unreliable for this UI).

*Landed 2026-07-19 (f039a87): ``ui/WorldBuilder/AgentBuildObserver.jsx``
over ``api.agentBuildEvents`` (SSE-over-POST, same framing as enrichRun
but every event is delivered — terminal included — and a dropped stream
returns null so the observer resumes from last-seen ``i``+1). Todo panel
from the latest turn event, current-action strip with the transient
enrichment progress line, action log with one-line result summaries +
expandable raw JSON, findings rendered inline (severity, kind,
suggestion) from evaluate results and done-gate rejections, terminal
banners incl. accepted/auto-accepted findings, cancel, reconnect-on-drop
and artifact replay for finished builds. Browser verification is Filip's
live test (which doubles as C2's recorded live run); note his
long-running dev backend must RESTART to pick up the C2 python routes —
module JSX reloads from disk, the harness does not.*

### C4. Ideation — conversational world definition — size L

The new front door: a chat-shaped flow replacing the button-initiated
`WorldPromptInterview` rounds — part of the flow, not an optional
affordance. Rules-first: the first converged artifact is the handful of
world rules that define the world (D4). The AI decides when the idea
feels settled and offers the go prompt; the go-ahead hands the brief to
the harness. The classic wizard remains available unchanged (P5).
Sequenced last so an end-to-end agent mode exists early; C3/C4 have no
dependency on each other and can swap if the front door starts to matter
more.

*Landed 2026-07-19 (b73c275), with three decisions settled with Filip at
C4 start. (1) **The classic entry is disabled** — superseding this
section's "classic wizard remains available unchanged" line: Filip chose
one path to maintain, "for now". The pre-start screen is scenario +
prompt field + the `WorldIdeation` chat; Generate World, skip-review and
the interview UI are gone (the step-review machinery stays for resuming
pre-existing classic drafts; the server generation routes are untouched,
so re-enabling is a UI change). (2) **The offer highlights, never
gates**: Go is available from the first non-empty prompt draft
(zero-turn go = the old direct launch, brief with empty rules) and turns
primary when the model flips ``ready``. (3) **Interview removed
outright** (routes ``/prompt-questions`` + ``/fold-answers``, builders,
tests — B1's decided-removal precedent). Mechanics: one stateless
``/ideation-turn`` route (smartest slot, interview-route error
discipline); the completion is ``{reply, prompt, rules, ready}`` — the
drafts round-trip through the client every turn, so hand edits (prompt
field, per-rule ✕) are simply current truth; conversation state is
client-held in localStorage (relaunch-safe, PWA-kill precedent). The
brief rides ``state["brief"]`` (explicit metadata key in persistence),
renders into every turn's harness system prompt as fixed design
decisions, and shows in the observer. Rules feed ``world_rules`` at the
generation seam: its ``generate(ctx)`` override composes the facade's
new ``generate_declarative`` (factored from ``generate_step``'s tail,
exact parity — no-brief behavior byte-identical) and enforces agreed
rules verbatim at the head of ``custom_rules`` on every regeneration;
``RULES_DOCTRINE`` is the one rule-style text shared by step guidance
and the ideation prompt; ``patch_step`` loudly rejects a
``custom_rules`` patch that drops an agreed rule (P7). The live
conversation check is Filip's (with his C2/C3 live test; backend restart
still pending).*

### v2 extensions (recorded, deliberately unscheduled)

- Structural surgery tools — node/edge/connection surgery DESIGNED
  2026-07-19 and scheduled as v2a (see below); terrain edits split off
  as v2b, double-gated (live evidence + Filip's intended holistic
  terrain-system review, review first).
- A tool-looping evaluator (read tools, multi-step critique).
- Backend-restart resume (re-derive agent context from todo + world
  state).
- Play-time policy in the brief (old question 3's other half).
- Module-contributed tools (old question 4) — after the module-contract
  discussion.

### v2a — Structural surgery toolset (designed 2026-07-19)

*Design settled with Filip in conversation while C4 was in flight,
recorded so implementation starts from decisions, not re-derivation.
Originally gated on live-test evidence (D1: "if evaluation shows a
recurring wall"); Filip chose 2026-07-19, C4 having landed, to build it
directly — the still-outstanding live test then exercises the full v2a
toolbox too.*

**Two code findings shaped it.** (1) Addition already has a trusted
path: `persistence.append_map_node` (play-time location authoring)
handles both storage homes (child-map bundle vs `map_generation` step
data), enrichment write-cache coherence, and partner-region membership;
`_found_new_node` supplies anchor-relative placement. What v1 truly
lacks is remove, rewire, and any connection write surface — connections
live in two homes (root/parallel in step data, child maps in their
bundles, merged at compile). (2) The lints already detect surgery's
entire failure surface (dangling edge/connection, orphan, disconnected
map, unreachable map, broken link token), which is what makes S1's
two-tier validation sufficient.

**Decisions:**

- **S1 — Refuse hard, warn soft (P7 applied to surgery).** A mutation
  that would leave a dangling *reference* — removing a node a child map
  anchors on or a connection endpoint names, adding an edge to a
  nonexistent node — is rejected pre-execution with the blockers listed;
  the agent resolves them stepwise. Soft topology and content quality
  (map splits, inbound `${link_...}` tokens going stale, orphaning) are
  allowed, surfaced in the tool result, and owned by lints + the
  done-gate — mid-restructure worlds are transiently messy by design.
  Never silently repaired.
- **S2 — No session gate.** By design, sessions copy the world into the
  session save — a session never plays the template world directly — so
  surgery on a world cannot reach an existing session's state.
  `remove_node` therefore carries no session check.
- **S3 — A shared surgery surface; tools wrap it.** Validated mutations
  live in `worldgen/surgery.py` — add_node / remove_node / add_edge /
  remove_edge / add_connection / remove_connection — each validating,
  writing through persistence, invalidating the compiled cache, and
  returning a report (blockers refused, warnings surfaced). Agent tools
  in `agent/tools/structure.py` wrap these 1:1 (P5), keeping D1's
  agent-write-surface = user-write-surface property in the forward
  direction: a future human map editor gets identical invariants for
  free.
- **S4 — Unnamed adds are legal.** `add_node` may create unnamed nodes
  (enrichment fills them; the majors-floor lints keep them visible).
  Placement is always anchor-relative — the `_found_new_node` route-leg
  computation promoted to a shared home — and the tool never accepts raw
  coordinates, keeping bounds/terrain validity out of the agent's hands.
- **S5 — Terrain edits are v2b, double-gated.** Targeted terrain edits
  are the one genuinely new machinery (masked pipeline re-runs) and the
  heaviest invariant carrier (nodes are placed on the current raster; no
  lint detects a stranded settlement). Whole-map regeneration already
  exists via `run_step`. v2b waits for (a) live evidence of demand and
  (b) the holistic terrain-system review Filip intends — the review
  comes first.

**Tool sketch** (all `mutates: true`, under the existing D5 budgets — no
new budget classes):

- `add_node(map_id*, near_node_id*, name?, type?, importance?,
  label_description?, description?, edges_to?=[anchor],
  region?=partner's)` — name uniqueness via `join_key` as in
  `edit_node`; writes through `append_map_node`.
- `remove_node(node_id*)` — refuses on child-map anchors and connection
  endpoints (S1); cascades its edges and region membership (center-node
  reassignment included); reports nodes whose descriptions link to it
  and lost `contained_locations` bindings; warns on map split. Needs the
  new persistence mirror of `append_map_node`'s dual dispatch.
- `add_edge` / `remove_edge` — same-map endpoints, no duplicate edges;
  removal warns on orphan/disconnect. Accepted limitation: new edges get
  no road polyline (roads are generation-time artifacts;
  `append_map_node` edges already behave this way).
- `add_connection` / `remove_connection` — minimal surface (from/to
  endpoints, kind, name, description, bidirectional; defaults for
  travel/requirements/hidden). Storage ownership mirrors expansion: a
  connection touching a child map lives in that child's bundle,
  root↔root lives in step data. Removal warns when it leaves a map
  unreachable.
- `edit_node` grows `type` / `importance` — the existing
  `save_node_enrichment` path (`_persist_generated_start` already writes
  both fields through it).

*Landed 2026-07-19 (51e8c0d), with recorded refinements against the
sketch. (1) `add_node` has NO region argument — the node inherits its
anchor's region and `_append_to_partner_region` handles membership; with
two region representations (node.region strings vs regions[].node_ids
lists) an explicit override was a silent-mismatch trap. Placement
anchors are the whole edge-target set (centroid step), not just
`near_node_id`. (2) Step-data connections turned out to be
legacy-LayerConnection-shaped (fresh worlds included — migrate converts
at every compile), so root↔root additions get a NEW home instead of
writing legacy records: the `world_connections` metadata key (native-v2
ConnectionRecords, C4's `brief` round-trip precedent), folded into
`compiled["connections"]` post-migrate with id dedupe. Child-touching
connections land in the to-side bundle. New connections fix
`travel={"mode": "instant"}`, `origin: "surgery"`. (3)
`remove_connection` serves all three homes — metadata key, child
bundles, and the legacy step-data list (clearing the endpoints'
`interlayer_connection_id` stamps); a migrated record whose compiled id
was synthesized (id-less legacy input) is refused loudly with a
regenerate-the-step pointer, per P7. An anchored child map never warns
unreachable on connection removal — the parent anchor keeps it
attached, by design. (4) The enabling promotions: `grow_position` →
`mapmodel` (removing `start_locations`' private
`MapExpansionEngine._grow_position` import), `connected_components` /
`unreachable_maps` / `connection_endpoints` → `mapspace` (lints import
them now), the `LINK_TOKEN` scan regex → `enrichment/context.py`. (5)
S2 confirmed in code while implementing: `migrate_session_state`
operates on the session's own `world_data` copy. Verified by 18 tests in
`test_agent_surgery.py` (per-tool happy paths + every refusal + all
three connection homes) plus the module-by-path and root suites; a
scripted five-op live sequence (add node, add portal, split-warn remove,
healing edge, refused endpoint removal) rendered for Filip in chat.*

### v2c — Checkpoint/revert: the agent's undo (designed and landed 2026-07-19)

*Designed with Filip 2026-07-19 ("can we give the AI tools to revert
changes that it did wrongly"), built on his go the same day together with
the Ecstasy Veil fixes. That build's turns 11–17 are the motivating
specimen: a silently-destructive ``map_generation`` re-run replaced a
finished, fully-authored root map; the agent burned five turns diagnosing
and then "restored" the map from conversation memory — producing a
sibling it could not tell from the original, and reporting success.*

**Decisions:**

- **R1 — Snapshots, not inverse operations.** Surgery tools have natural
  inverses, but the heavy tools' inverse ("undo this step re-run") IS
  "restore the previous bytes", and partial rollback of interleaved
  mutations is a merge problem an LLM mid-build will get wrong. A world
  is a plain directory (metadata + step files + child-map bundles +
  sites + terrain rasters) and ``load_world`` reads it fresh — so a
  checkpoint is a byte-exact directory copy and restore is total,
  linear, honest.
- **R2 — The harness checkpoints automatically before every mutating
  tool call** (``ToolSpec.mutates`` is already declared), keyed by the
  action's log index; the observation echoes the checkpoint id. Auto,
  never agent-invoked: the failure mode is precisely that the agent does
  not know it is about to do something destructive (the Ecstasy Veil
  agent believed it was *creating* a child map). A failing snapshot
  blocks the mutation loudly instead of running it unprotected (P7) and
  costs no budget.
- **R3 — Revert rewinds world content only.** Todo, observations,
  budgets and finding-round tracking keep going forward — ``git
  revert``, not a time machine — and restore carries the CURRENT brief
  forward: the user's contract (rules, notes, amendments, verifier
  context, veto locks) is not the agent's work product to roll back.
  ``discuss_finding`` outcomes are therefore effectively non-revertible,
  by design (the veto is the user's instrument there, N7).
- **R4 — History never rewinds.** ``agent_build.json`` and the
  checkpoint store itself are excluded from snapshot and restore both
  ways.
- **R5 — The window is strictly per-build.** Cleared at launch (stale
  tags from a previous build on an adopted/vetoed world would collide
  with fresh action indices) and on every terminal state. Revert is
  itself a mutating action — checkpointed, budgeted, revertible; no new
  budget class (D5 bounds any revert loop).

*Landed 2026-07-19 (60889d7): ``persistence.snapshot_world`` /
``restore_world`` / ``list_checkpoints`` / ``clear_checkpoints``
(``_checkpoints/`` inside the world dir). Snapshot flushes the
enrichment write cache first so pending node writes are captured;
restore invalidates that cache BEFORE replacing files — a later flush
must never resurrect the abandoned timeline — and drops the child-node
index; the revert tool then invalidates the facade-owned compiled cache.
The build-scoped ``revert`` tool is the 20th catalog entry; the system
prompt teaches "revert instead of rebuilding lost content from memory".
Verified by 12 new tests: checkpoint-store roundtrip over every content
home incl. terrain rasters, history exclusion both ways, brief
carry-forward, flush/invalidate coherence, and the real loop
(mutate→revert→byte-equal, read-only actions snapshot nothing, launch
clears stale tags, a failed snapshot blocks the mutation).*

### v2d — Child-map expansion tool (designed and landed 2026-07-19)

*Prompted by Filip's question after the Ecstasy Veil diagnosis — "is
there a good reason for there not being an expansion tool?" — and the
honest answer was no. The gap was an artifact of how D1's v1 list was
enumerated (steps, passes, and the wizard's write surfaces; expansion
triggers from routes and play-time, outside the step list — exactly the
Crucible Stars blind-spot shape), not a withholding decision: the
rationale that withheld surgery (no existing surface, heavy invariants)
never applied here, since ``facade.expand_node`` is the cached,
invariant-handling, terrain-aware surface play-time expansion and the
pregenerate pass already trust. D1's own evidence gate ("if evaluation
shows a recurring wall") was met twice over — the child-map wall is the
documented proximate cause of both live-build failures. Filip approved
build without design confirmation; decisions below were made in
implementation and recorded here.*

**Decisions:**

- **X1 — One primitive, no batch.** ``expand_node(node_id, level_type?,
  force?, note?)`` wraps the facade surface 1:1 (the v2a
  wrap-the-shared-surface pattern). Deliberately no ``run_pregenerate``
  batch tool: the hierarchy's pregenerate plans (readable via
  ``read_step``) are a work list the agent drives one node per action —
  one observation, one v2c checkpoint and one budget unit per child
  map, instead of one long opaque call.
- **X2 — Loud expandability, with the fix in the message.** Unknown
  node, unnamed anchor (name it: label pass / edit_node), depth
  exhausted, no child level in the hierarchy (regenerate
  hierarchy_design with a note), ``level_type`` not allowed below this
  map (the allowed list is in the rejection). An existing child is NOT
  an error: it returns as-is, flagged ``existed``, with a pointer at
  ``force``.
- **X3 — force is in v1, and it is steerable.** A bad child map needs
  regeneration, and v2c makes that safe (checkpointed, revertible). The
  ``note`` threads into the child author's prompt — via the context
  dict, not the signature (``_live_expand`` is an established test
  patch point; the C1 guidance-channel precedent) — because a force
  regeneration without steering would be the Ecstasy Veil unsteerable
  re-roll again. ``facade.expand_node`` gains ``user_note`` for every
  caller. A force regeneration invalidates the compiled cache (its
  in-place update only ever adds maps/connections; a replacement behind
  its back would serve a blend).
- **X4 — Results tell the naming truth.** Authored levels come fully
  named/described ("no label pass is pending"); procedural children are
  born unnamed (detail lazily, or ``run_pass`` scoped to the new
  map_id); terrain-flagged children report that they rasterized their
  own terrain — which is also how per-planet terrain is reached (the
  Crucible Stars honest-terrain contract: children rasterize at
  expansion time).

*Landed 2026-07-19 (e7d2b33): ``agent/tools/expand.py`` (the 21st
catalog entry), ``user_note`` through ``MapExpansionEngine.expand`` /
``facade.expand_node``, the system-prompt line naming expand_node as
the ONLY source of child maps (map_generation cannot add them — the
exact confusion that destroyed the Ecstasy Veil root), and a
"Child maps" how-to-work bullet. Verified by 4 new tests: create +
read-tools round-trip (anchor stops advertising expandable), cached
return + force regeneration, the rejection set, and the note reaching
the child author's prompt. With v2d, the Crucible Stars systemic open
item ("exposing the expansion/pregenerate phase as agent tools") is
closed; per-planet terrain arrives by modeling planet-likes as
terrain-flagged children, and the parallel-map-raster question stops
gating anything.*

### C5 — Ideation notes, the note verifier and the review gate (designed 2026-07-19)

*Designed 2026-07-19 with Filip directly after v2a, build started on his go
the same day. Motivating observation (Filip's): C4's funnel records
everything the ideation conversation converges on into two artifacts — a
short seed prompt and 3–7 rules — and both are global, so converged detail
either dies at the Go boundary (the transcript is discarded) or
contaminates the whole world (a rule agreed while discussing one desert
planet becomes part of the rubric the ocean planet is judged against).
Also settled in that conversation: this is within-world machinery only —
no cross-world concept library — and binding granularity is per-map.*

**Decisions:**

- **N1 — The brief grows notes; rules stay the rubric.** A note is an
  established fact or decision from the ideation conversation that is
  neither seed direction nor a rule: `{"text", "subject"}`, where a
  missing/empty subject means world-scoped and a named subject ("the sand
  planet Kharos") scopes it to the thing it names. The ideation model
  maintains the notes as a third shared draft (same round-trip discipline
  as prompt and rules: shown beside the chat, hand-editable, the client's
  version is current truth). Rules keep their D3/D4 role unchanged — small,
  global, the evaluation rubric; notes are facts, not rubric bullets. At Go
  the notes get stable ids (`n1`..`nk`) and ride the brief.
- **N2 — Subjects bind per-map, loudly.** A subject binds to the map whose
  label/id it names, or the map containing a named node it names
  (`join_key` normalization, exact before containment — the
  authored-location-binding precedent). Binding is computed against the
  compiled world whenever needed, never stored as build-time truth: maps
  appear and rename mid-build. A subject note that binds to nothing is a
  deterministic blocking lint finding (`note_unbound`, P7) — the fix is to
  build the thing (hierarchy steering, surgery) — and mid-build it is
  simply an honest "doesn't exist yet" signal.
- **N3 — Injection follows one visibility rule: the orchestrator sees
  everything, content calls see their scope.** The build agent's system
  prompt renders all notes in full (grouped world/subject) — the builder
  needs the whole contract. Generation calls get scoped context: world
  notes join the seed seam (`seed_with_scenario`, so steps, list rerolls
  and expansions all see them) plus a one-line index of subject notes so
  nothing is invisible; subject notes inject in full only into their
  bound map's content calls (enrichment context block rendered by the
  passes, child/abstract map expansion) — hierarchy_design, which must
  create the subjects, gets them all.
- **N4 — The note verifier is a second, read-only agent.** The recorded
  v2 "tool-looping evaluator" upgrade arrives scoped to notes: a bounded
  loop (own turn budget) whose toolset is carved mechanically from the
  registry — `mutates=False`, minus `evaluate`/`discuss_finding` — so
  future read tools flow to it (P2). In: the note checklist with current
  bindings. Out: per-note verdicts (honored / not honored, evidence,
  suggestion). Not-honored verdicts become blocking findings
  (`note:<id>`) beside lint and critique findings; the rules critique
  call stays exactly as landed. Offline the check degrades to the
  binding lint (the gate stays testable without tokens); the loop's turn
  function is a module-level patch point like `agent_turn`.
- **N5 — `discuss_finding`: the builder may argue; the verifier serves
  the note's author.** A registered tool (the catalog is the discovery
  surface, P1) the builder can call on a note finding: the harness routes
  the message to the verifier, which holds a per-finding transcript, may
  re-read the world, and answers with a structured outcome — `upheld`
  (stands, with reason), `withdrawn` (the builder's evidence convinced
  it; recorded so later runs don't re-flag capriciously), or `compromise`
  (amended note text). Bounded exchanges per finding; after the budget
  the finding stands. The verifier's stance is contractual — the note's
  author is the user, not the builder; amendments must serve the user's
  evident intent or resolve a genuine conflict (note vs note, note vs
  rules), never builder convenience — agent-to-agent sycophancy is the
  failure mode this stance exists to resist. A compromise updates the
  brief note in place (original text kept, `status: "amended"`, rationale
  recorded) and the build proceeds against the amended text; the whole
  exchange streams into the action log (the observer shows the argument).
- **N6 — Note findings are never auto-accepted.** The done-gate's
  fix-round auto-accept does not apply to `note:` findings: every note
  ends honored, honored-as-amended, or *explicitly* accepted with a
  recorded reason in the done claim. Explicit accept stays available as
  the escape hatch, so builds still terminate (the turn budget bounds
  everything else).
- **N7 — The review gate sits at the absolute end; a veto has teeth.**
  User steering stays at the boundaries (P6): ideation, Go, and now one
  review moment after the build finishes. The world completes and saves
  regardless — not vetoing (or ignoring the offer) means done, no action
  required. When the done result carries amendments or explicitly
  accepted note findings, the observer renders them — original vs what
  happened, per-item veto. A veto restores the original text as binding,
  marks the note `no_compromise` (a vetoed note can never be amended
  again), and relaunches the agent on the finished world as a normal
  bounded build (the existing adopt-a-world path) whose brief flags the
  vetoed notes as the work list — same loop, same gate, same review if
  new compromises appear.

*Landed 2026-07-19 in five commits (36a32db notes core, 6f25793
verifier, 8e8713d dialogue, f7ba87a review gate, 903bca4 frontend), with
refinements recorded against the sketch. (1) Binding resolves in four
tiers — exact join_key on map label/id, exact on a named node (its map),
containment on map labels, containment on node names — and a tie WITHIN
a tier is ambiguity: reported unbound with the candidates listed, never
guessed. Note finding keys carry no map (``note:<id>:-:-``) so a
binding change cannot reset fix-round tracking. (2) Loud degradation
gained a second leg: a verifier that exhausts its turn budget (setting
``world.note_verifier_max_turns``, default 16) reports every unchecked
note as a blocking ``note_unverified`` finding — never a silent pass.
(3) ``discuss_finding`` is build-scoped through a new
``ToolContext.build`` field (rejects invocations outside a build);
exchanges budget via ``world.note_discussion_rounds`` (default 3);
inside one exchange the verifier gets ``DEFAULT_DISCUSSION_TURNS`` (6)
read/answer turns, and budget exhaustion upholds. A withdrawal records
``verifier_context`` on the note itself, so later verification runs see
the resolution (the world, not the handle, is the memory — it survives
the fix-run relaunch). (4) Both note-obligation key shapes share the N6
exemption: verifier findings (``note:*``) and the ``note_unbound`` lint.
(5) The compiled world carries the brief through (compiler copy, the
scenario precedent), which is what lets enrichment context, the lints
and the passes read notes with only the compiled dict in hand; the
observer streams ``verifier_action`` progress lines and renders the
review panel from ``result.pending_review``. Suite: 4 new module test
files (45 tests) + module-by-path and root suites green; the live
conversation + build check rides Filip's outstanding C2–C4 live test,
which now exercises notes end to end.*

### C6 — World explorer + classic-system removal (landed 2026-07-19)

*Decided with Filip 2026-07-19 ("complete rip-out if we can do that since
it's a legacy system"; recovery lands on the agent flow; node editing
deferred; collapsible drawers on mobile). Two commits: the additive
explorer, then the rip.*

**The explorer.** The post-creation world view is now
`ui/WorldExplorer/WorldExplorerScreen.jsx`: the map fills the screen
(MapRenderer, double-click descends), an elements panel on the left
lists everything the current map contains — nodes with inline detail
(description, `additional_details`, ways out, enter-child), regions,
ways in/out with hidden badges (author view — only the player overlay
filters hidden), child maps, and the C5 notes bound to the map
(client-side join_key approximation, exact + containment tiers; the
lints stay the binding authority) — and a global panel on the right
holds expandable entries: brief (with per-subject bound/unbound hints),
every info step via SchemaForm with edit/save and regenerate-with-note,
terrain preview, the EnrichmentPanel (now hosted step-free — the
"one-shot worlds have no enrichment-panel host" follow-up is moot for
saved worlds), and interior pre-expansion. Selection syncs list↔map
both ways (`MapRenderer` gained an `onNodeSelect` callback). Panels
collapse; on small screens they are slide-over drawers.

**The read surface fixed a real gap.** The explorer reads a new
`GET /api/world/{id}/compiled` — compiled fresh from disk (never through
the size-1 cache: a browse must not evict the actively-enriched world or
decompress terrain rasters; private keys stripped). The old review
screen rendered `map_generation` step data and could never show
post-generation child bundles or surgery connections.

**World-scoped regenerate.** `POST /api/world/{id}/regenerate-step/{step}`
loads the world from disk, runs `generate_step` with full chain context
(brief included, so world_rules keeps its agreed-rules enforcement),
persists that one step, invalidates the compiled cache. It refuses
map/terrain/enrichment steps loudly (D1's withholding, restated at the
route). The old session route's `_auto_save_draft` used to create a
phantom "In Progress" duplicate when re-rolling a loaded world — the
session machinery is simply not involved anymore.

**The rip.** The wizard's sequential flow, StepCard, the step-UI
registry, MapStepView, EnrichmentStepView and WorldReviewScreen are
deleted; WorldBuilderWizard slimmed into `WorldCreateScreen` (prompt +
scenario + ideation + observer, which gained an Explore-world action).
Server side every classic session route died — generate, continue,
generate-step, regenerate-item, approve-step, state, debug/skip-to,
compile, save, discard, resume, enrich/commit — along with
`world_gen_sessions`, auto-save-draft, the enrichment→draft sync and
dynamic-skip pruning. P5 holds: `generate_step` and the pass engine are
untouched, driven by the agent, the explorer and tests.

**Recovery lands on the agent flow.** `agent/build` now exposes the
harness's existing `world_id` adoption; `/world/list` carries
`has_agent_build` (artifact presence). An in-progress world's primary
affordance is "View build" (reattach observer) or "Finish with AI"
(adopt build); Explore stays available read-only.

**Recorded consequences, deliberately accepted:**

- `pregenerate_planned_maps` lost its last route call site (it lived in
  the removed `enrich/commit`, itself UI-dead since B1.5). The Crucible
  Stars open item — expose the expansion/pregenerate phase as agent
  tools — is now the *only* path to planned child maps, unchanged in
  urgency.
- Regenerating a design step on a saved world does not prune data of
  steps the new design skips (the pruning was wizard-approval machinery);
  the agent's `run_step` has had the same behavior since C1. Stale
  skipped-step data still compiles in — a lint or a prune-on-write is
  the fix if it bites.
- `skip_review` and one-shot mode are gone as behaviors; the metadata
  key survives only as inert legacy passthrough in persistence.
- Pre-C4 classic drafts lost their step-review resume; their recovery
  is adoption ("Finish with AI"), which preserves content and brief.

**Verify:** module suite (543) + root suite (688) green;
`test_explorer_routes.py` covers compiled/regenerate/adopt/recovery
routing; driven in real Chromium against a seeded, hand-decorated world
(desktop three-pane, node selection sync, child-map descent, brief
binding badges, mobile drawers, edit round-trip persisted to disk,
recovery buttons on the world list).

### Live-run findings — the Crucible Stars build (2026-07-19)

The first full live agent build (Filip's C2/C3 test; cancelled at turn
28) surfaced two silent walls with one shared shape: **capabilities the
classic flow triggers outside the step list are invisible to the agent.**
Full diagnosis from the build artifact + LLM call log, recorded here
because it gates several open decisions:

1. **Per-planet terrain is unreachable.** The hierarchy modeled the three
   planets as ``parallel_maps``; ``terrain_generation`` builds only the
   root raster — its ``_layer_specs`` still read the deprecated
   ``layer_design`` step while its catalog text promised "each surface
   layer". Four re-runs (one with an invented ``config.layers``, silently
   ignored) returned the same single ``main`` layer; the agent then
   fabricated three layer entries via ``patch_step`` — accepted, because
   terrain was not in ``_UNPATCHABLE_STEPS`` — and ``natural_landmarks``
   authored features against geography no raster backs. P7's
   error-feedback loop never fired because every call succeeded.
2. **Pregenerate child maps are unreachable.** The hierarchy's three
   ``pregenerate`` entries are built by ``pregenerate_planned_maps``,
   invoked from routes *after* generation — no agent tool runs it. Five
   ``map_generation`` re-runs (wiping labels twice) hunted child maps
   that cannot appear; ``read_node`` even advertises nodes as
   "expandable" with no tool to expand them.

**Landed same day (784b65c), the narrow honest-terrain fix:**
``terrain_generation``'s docstring + catalog description state the real
contract (one root raster; terrain-flagged children rasterize at
expansion time; re-running cannot add layers); ``terrain_generation``
added to ``_UNPATCHABLE_STEPS``; tests pin ``_layer_specs`` (hierarchy
world → root only; legacy ``layer_design`` data keeps multi-layer) and
the patch rejection.

**Open discussion items (deliberately not scheduled):** exposing the
expansion/pregenerate phase as agent tools (the systemic fix — without
it any world needing child maps or non-root terrain is out of reach) —
*landed 2026-07-19 as v2d (e7d2b33)*; whether ``parallel_maps`` should
carry creation-time rasters, or whether planet-likes belong as
terrain-flagged children instead (v2d defuses this: terrain-flagged
children work today, so it stops gating anything); a lint for
terrain entries with no raster on disk (would have caught the
fabrication at the done-gate); rejecting unknown ``run_step`` config
keys (P7 at the config layer) — *landed 2026-07-19 with the Ecstasy
Veil fixes (5174f95), next section*; a sanctioned record-a-blocker move
so a walled agent surfaces the wall instead of patching around it.

### Live-run findings — the Ecstasy Veil build (2026-07-19)

Filip's second live build (an erotic sci-fi galaxy of eight worlds;
artifact exported at turn 23 mid-run) hit the Crucible Stars child-map
wall again — and the diagnosis from the build artifact + LLM call log
surfaced four NEW defects, all in the silent-contract family P7 exists
to prevent. All four were fixed the same day; the run also settled v2c.

1. **A finished root map destroyed by an invented, silently-accepted
   config.** Turn 11: hunting a child planet map, the agent called
   ``run_step("map_generation", config={"parent_node_id": ..,
   "level_type": "planet", "generator_id": .., "total_nodes": 25})``.
   None of those keys exist; nothing rejected them (``total_nodes`` was
   even below the schema's own minimum) — the step re-ran the abstract
   root author and REPLACED the finished 20-node, fully-named-and-
   described root with a fresh 14-node map. The call "succeeded".
2. **The steering note never reached the map author.** Turn 17, the
   recovery: regenerate with a note listing the remembered nodes
   ("restoring the original 20 locations: Helios Prime …"). All three
   ``map:abstract`` calls of the build had BYTE-IDENTICAL inputs
   (tokens_in 2874 each): ``generate_step``'s USES_MAP branch dropped
   both ``user_note`` and ``config`` on the authored/abstract paths —
   the advertised D1 recovery instrument went nowhere, for exactly the
   step where structural problems live. The partial-looking restoration
   was anchoring (authored places from lore/landmarks context), i.e. an
   unsteerable re-roll; node counts 20 → 14 → 20 across identical
   prompts are sampling variance, nothing more.
3. **The evaluator manufactured a false blocking finding.** Turn 21's
   critique flagged "The map claims 20 locations but only 12 are
   listed" — a contradiction the excerpt builder itself created:
   ``_content_excerpts`` printed the full count in the map header, then
   silently sliced the node list to the 12 highest-importance entries.
   The model did what it was told with the fabricated mismatch; two
   turns burned re-reading and re-evaluating, and the false finding fed
   fix-round tracking.
4. **``run_step``'s map result lied about naming.** The abstract author
   names and describes every node at generation time, yet the result
   note said "Nodes are unnamed until the label pass runs" — in the
   same dict as ``"named": 20`` — sending the agent on a no-op label
   pass at turn 8.

**Landed same day:** 5174f95 — ``user_note`` threads into
``expand_root``/``expand_abstract_root`` and every abstract layer
prompt (steered map regeneration is now real); ``Step.config_schema``,
the declared generation-config contract (``schema`` describes OUTPUT
fields — the Ecstasy Veil config was invented precisely because no
contract said what config IS), declared for the two consumers
(``map_generation.total_nodes``, terrain ``resolution``/``biome_mode``),
rendered into the catalog, validated loudly by ``run_step`` via the
shared ``validate_params``; per-world steering honesty — ``total_nodes``
rejected on authored/abstract roots (the author picks its own count, cap
20), notes rejected on procedural map/terrain roots (no LLM reads
them); the truthful result note (actual unnamed counts). 39380ca — the
excerpt carries an explicit truncation marker with the numbers and the
critique prompt forbids findings inferred from the excerpt's own
bounds. 60889d7 — v2c (see its section): with checkpoints, turn 11's
destruction would have been one ``revert`` call instead of six turns
and a sibling map.

**Still open (unchanged from Crucible Stars):** ~~the expansion/
pregenerate tools~~ — *closed the same day by v2d (e7d2b33), on Filip's
"is there a good reason for there not being an expansion tool?": the
agent can now CREATE the child maps it was hunting in both live runs —
leaving* the missing-raster lint and record-a-blocker (parallel-map
rasters stop gating anything once planet-likes are terrain-flagged
children).

### C7 — One conversation: user messages in the build, ideation as the chat phase (designed 2026-07-20)

*Designed 2026-07-20 with Filip, from his proposal to merge the C4
pre-chat and the build into one flow ("you get into the agent view and
have a chat with the agent about what the world should be"),
reality-checked against the code in conversation. Both live builds
supplied the motivating pain: Filip could SEE the agent walled in the
observer and had no move but cancel. Nothing here is built yet; the
decisions below are settled, two forks are explicitly open.*

**The shape.** World creation becomes one continuous agent session with
two phases and the existing gate between them. The session opens in a
**chat phase**: a message-paced conversation about what the world should
be, in which the model maintains the brief (prompt, rules, notes)
through a restricted tool catalog. The Go button stays: pressing it
flips the same session into the **build phase** — the current
self-driving loop, full catalog, budgets, done-gate — and the
conversation channel stays open: the user may type at any time, and the
message lands at the next turn boundary. C4's decisions carry over
unchanged: the ready offer highlights and never gates, and zero-turn Go
(type a prompt, press build) survives.

**Decisions (settled with Filip, 2026-07-20):**

- **U1 — One session, two agents.** One harness session, artifact,
  event stream, message channel and UI surface across both phases — but
  two separate agents with different system prompts (design partner vs
  builder). "The same AI" is UX truth, not prompt truth: forcing one
  prompt to do both jobs risks a builder that chats instead of acting
  and a partner that reaches for build tools. What unifies is state and
  transport, not persona.
- **U2 — Brief edits are tools, in both phases; the gate is the
  description, not machinery.** update_prompt / update_rules /
  update_notes (granularity an implementation choice; 1:1 with the
  three drafts is the default) are registered tools available to BOTH
  agents, and their descriptions state the rule: use only in response
  to direct user input, never unprompted. In the chat phase the
  constraint is vacuous (every turn follows user input); it exists to
  bind the build agent. This is what makes a mid-build "actually, make
  the ocean frozen" coherent with C5: the agent edits the note through
  the tool, and the verifier — which computes bindings and verdicts
  fresh from the brief — judges the world against the CURRENT contract.
  No amendment machinery, no authority levels.
- **U3 — User messages carry no engineered authority.** A mid-build
  message is injected verbatim at the next turn boundary as a plain
  observation (`User said: "..."`) — the cancel-flag semantics applied
  to input. No binding/advisory distinction in code, no harness
  enforcement of what the agent does with it: the model is trained to
  follow instructions, and that is the mechanism (Filip's call). The
  designed fallback if live runs show the builder editing the brief
  unprompted: a cheap harness gate (brief-edit tools valid only within
  a few turns of a user message) — recorded, not built.
- **U4 — The transcript persists; the builder reads it on demand, never
  ambiently.** The chat phase is the first stretch of the session log,
  so "keeping the transcript" is a prompt-policy question, not a copy.
  It does NOT enter the build agent's turn prompt: a transcript
  contains rejected ideas beside kept ones (a second source of truth
  against the brief), ambient availability softens the pressure that
  makes ideation record notes (the verifier enforces notes, not chat),
  and the no-truncation rule (P9) would make a long chat tax every one
  of ~40 build turns. Instead a read-only registered tool
  (read_conversation) over the session's own log serves the moments
  that need it — the merged UI presents one continuous conversation, so
  "like we discussed…" WILL be typed — and doubles as recovery for
  mid-build instructions that scrolled off the RECENT_LIMIT window. The
  note verifier inherits the tool through N4's mechanical carve,
  anchored by its contractual stance. The ideation prompt keeps
  teaching that only prompt/rules/notes survive Go ambiently; the tool
  is a safety net, not the carrier.
- **U5 — A user-directed note edit is a hand edit, not a compromise.**
  No ``status: "amended"``: the N7 review panel exists for changes the
  AGENT negotiated, and must not present the user's own mid-build
  instruction back to them as something to veto. The audit trail is the
  action log (the message and the tool call it triggered) — hand-edit
  equivalence, the tool as the user's hand moved by their words.
- **U6 — Protocol: ``say`` joins the completion; one action per turn
  stays.** ``say`` is the user-facing reply channel, distinct from
  ``thought`` (internal; the observer shows it as the log's italic
  line). Chat-phase completions are ``{say, action?, ready?}`` —
  ``ready`` keeps its C4 meaning as the go offer; build-phase
  completions gain an optional ``say`` for answering a mid-build
  message (rendered as a chat bubble). Drafts do NOT ride the
  completion (Filip decided tools — superseding the
  drafts-ride-the-completion alternative the todo precedent suggested):
  a typical ideation turn is one completion; reply plus two draft edits
  is three. Each user message opens a small turn-budgeted mini-loop (a
  structural setting, loud on exhaustion, P9); D2's one-action-per-turn
  is untouched.

**Open forks (explicitly Filip's, before or during build):**

1. **May a user message edit a ``no_compromise`` note?** The veto
   stance (N7) says a vetoed note can never be amended again — but that
   guard exists against agent-to-agent lawyering, and here the note's
   AUTHOR is speaking. Lean: yes — the veto binds the agents, not the
   user. Undecided.
2. **Session start and draft litter (C7b).** The session needs a world
   id from its first event (the artifact lives in the world dir), so
   abandoned ideations start leaving embryonic drafts behind — today
   abandoning costs nothing (client-held). Lazy-create at the first
   chat message (or at Go, whichever comes first) is the default; the
   cleanup story (hide-from-list, sweep, or explicit discard) is an
   implementation decision to make when C7b starts. *(Resolved at C7b
   build time: lazy-create at the first message, and the cleanup story
   is EXPLICIT DISCARD — ideation drafts are visible in the world list
   as "In design" cards with Continue/Delete, and the session screen
   carries a confirm-gated "Discard draft". No hiding and no sweep: an
   abandoned ideation is a resumable conversation by construction (the
   artifact carries the transcript), and a sweep would delete it.)*

**Staging — two slices, the first lands alone:**

- **C7a — the mid-build channel (S–M).** A message queue on the
  AgentBuild handle, drained at the turn boundary into ``recent`` and
  the persisted log (new ``user_message`` event); ``POST
  .../agent/message``; the observer gains an input box and a "queued —
  the agent reads it after the current action" state (mid-enrichment, a
  reply is minutes away); optional ``say`` in the build protocol,
  rendered as a chat bubble; the U2 brief-edit tools and U4's
  read_conversation (over the mid-build exchanges — the chat phase
  joins it in C7b); system-prompt guidance (respond to messages, fold
  standing instructions into the todo, the brief stays the contract);
  the P6 rescope above. Messages spend nothing themselves; the turns
  reacting to them count as any other (a chatty user visibly burns the
  build's budget — revisit only if live runs show it hurting).
  Independent of C7b, immediately useful (both live builds wanted
  exactly this), and the live evidence de-risks the merge.

  *Landed 2026-07-20, with recorded refinements. (1) The channel is as
  designed: ``AgentBuild.post_message`` queues (ids ``m1``…), the drain
  at the top of each turn appends verbatim ``{"user_message": ...}``
  entries to ``recent`` and emits persisted ``user_message`` events
  (id + the turn that reads them); messages still queued when the build
  ends drain in the terminal path flagged ``unread`` — recorded, never
  silently dropped — and the status snapshot carries
  ``queued_messages`` so a reattaching observer shows pending bubbles.
  (2) ``say`` is accepted on both completion shapes and rides the turn
  event only when non-empty. (3) The brief tools are ``mutates=True`` —
  which is what excludes them from the verifier's N4 carve; the v2c
  auto-checkpoint before them is harmless since restore carries the
  current brief forward (R3). ``update_prompt`` writes ``brief.prompt``
  AND ``seed_prompt`` together (the generation seams read seed_prompt —
  a brief-only edit would be cosmetic), and the build system prompt now
  renders the brief prompt from the per-turn world_state re-read (D4),
  so the edit takes effect next turn. ``update_rules`` replaces
  wholesale, reports an added/removed diff, and points at re-running
  world_rules when it is already authored (the critique rubric is the
  authored step, not the agreed list). ``update_notes`` is id-matched
  wholesale: id-only keeps, id+fields edits, no-id adds, omission
  removes (reported loudly in the result); a user-directed edit clears
  ``status``/``original_text``/``rationale``/``verifier_context`` (U5 —
  a hand edit, not a compromise, nothing left for N7 to review), and
  fresh ids never reuse an id removed in the same call (verdicts and
  finding keys ride note ids). (4) Fork 1 stays open, held
  conservatively: ``update_notes`` refuses to edit or remove
  ``no_compromise`` notes — U3 means the tool cannot verify a user is
  speaking, so allowing it would reopen exactly the agent-side
  lawyering N7 locks out; lifting the refusal is a one-line change once
  Filip decides. (5) ``read_conversation`` reads the live handle's log
  and falls back to the persisted artifact — the fallback is what hands
  it to the note verifier through N4's mechanical carve (verifier
  ToolContexts carry no build handle). (6) Observer: input box while
  running, queued bubbles reconciled by message id at render time
  (replay-race-proof), user/say chat bubbles inline in the action log,
  the unread annotation on builds that ended mid-conversation, and a
  brief-display refetch after each successful brief-edit action so the
  contract on screen stays current. Verified by 14 new tests
  (``test_agent_conversation.py``; module suite 581, root 688) and a
  real-Chromium drive of the whole loop against the real app with only
  ``agent_turn`` scripted: queued state → drain → say + update_rules
  with the header refresh → a second message left queued → unread at
  terminal → artifact-replay reattach.*

- **C7b — the merged front door (L).** The two-mode loop (chat phase
  awaits the message queue; Go flips to self-driving — the control-flow
  rewrite of ``_run_build``); session-from-first-message with a lazily
  created draft world; the restricted chat catalog (brief tools +
  read_conversation) and the design-partner system prompt (today's
  ``build_ideation_turn_messages``, reshaped to the U6 protocol); notes
  get their stable ids at Go exactly as N1 specifies; the transcript
  persisted in the artifact makes the chat phase
  resumable-by-construction after a backend restart (recreate the
  session from the artifact on the next message — the build phase's
  restart resume stays a recorded v2 item); hand edits of the drafts
  (prompt field, per-rule/per-note ✕) become a small server surface,
  since the drafts are server truth now; one continuous screen (chat +
  editable draft panels + Go, growing todo/log/findings after Go, the
  input box persisting throughout). Net deletions: the stateless
  ``/ideation-turn`` route, ``build_ideation_turn_messages``, and
  WorldIdeation's localStorage transcript machinery.

  *Landed 2026-07-20, with recorded refinements. (1) An enabling fix
  came first: every brief-edit surface (the C7a tools, the N5
  compromise/withdrawal path, the N7 veto) wrote the brief through
  ``save_world``, whose ``in_progress=False`` side effect silently
  flipped a draft to finished on every brief edit — fatal for chat
  worlds, whose only mutations are brief edits. ``update_brief`` is
  the metadata-surgical writer (brief/seed_prompt/agent_phase only;
  draft status, ``draft_complete`` and ``created_at`` survive), and
  everything brief-shaped now rides it. (2) One session task,
  ``_run_session``: chat phase → Go flip → the untouched build loop,
  with ONE terminal path (unread drain, checkpoint sweep, done event —
  now phase-stamped). U1's two agents are two module-level patch seams:
  ``chat_turn`` beside ``agent_turn``. Chat completions are U6's
  ``{say, action?, ready?}``; each user message opens a mini-loop
  budgeted by ``world.agent_chat_turns`` (default 6, loud on
  exhaustion), the restriction to the four chat tools is enforced
  harness-side (the registry is unchanged — the build tools exist,
  they are just not this agent's), and chat LLM failures surface as
  observations without killing the session (a conversation survives a
  failed reply; only the build escalates to abort). Chat actions are
  never checkpointed (v2c stays build-scoped; R3 excludes the brief
  anyway) and Go clears the checkpoint store — "cleared at launch"
  means the build phase's start. (3) Go outranks queued messages: they
  ride into the build and drain at its first turn boundary (C7a
  machinery). Go validates a non-empty prompt loudly; zero-turn Go
  survives as the direct ``agent/build`` route. (4) Resumability: a
  chat task torn down while running (backend shutdown) writes NO
  terminal event — the artifact stays a live snapshot, queued messages
  included — and ``resume_chat_session`` rebuilds the handle from it
  (transcript, message counter, stranded messages re-queued), invoked
  by the message, Go and brief-edit routes alike; a cancelled
  conversation revives the same way. ``metadata.agent_phase``
  ("chat"/"build") labels the world list ("In design" cards,
  "World in design" title fallback for the still-unnamed). (5) The
  chat prompt shows the full conversation every completion (P9) via
  ``exchanges_from_log``, shared with read_conversation — whose scope
  is now honestly "the session's whole conversation", ideation
  included, for both the build agent and the note verifier.
  (6) Frontend: AgentBuildObserver is the one continuous screen
  (phase-aware header, chat bubbles without turn chrome, "the build
  started" divider, editable draft panels PUTting to ``agent/brief``,
  ready-highlighted Go, confirm-gated Discard, the input box in both
  phases — sending into a dead chat session revives it and the
  observer reattaches past the stale terminal event); WorldIdeation.jsx
  and its localStorage machinery are gone, and the create form clears
  its prompt on session start (the draft owns it — a leftover would
  silently seed the next draft). Verified by 17 new tests
  (``test_agent_chat.py``; module suite 597, root 688) and a
  real-Chromium drive of the whole loop with only the two turn seams
  scripted: first message → session, tool-edited drafts, mid-chat
  relaunch replay, ✕ hand edit, Go, mid-build message → say, cancel,
  "In design" list card, continue-from-list, discard.*

**What C7 deliberately does not change:** the brief remains the
contract and the only ambient carrier across Go (C5's machinery and
stances are untouched); generation calls never see the transcript
(N3's visibility rule); budgets stay harness-enforced (D5); the chat
phase drives the same harness, not a second engine (P5); approval
gates stay dead — observability, cancel, and now a voice, but never a
mid-build gate (P6, rescoped accordingly in Design principles).

### Superseded: the plan-artifact design (refined and replaced 2026-07-19)

The original Arc C made the plan a step: a `build_plan` artifact authored
by an LLM from the B2 catalog, edited and approved by the user in a
catalog-driven editor (old C1b), walked by a validating executor, with a
bounded reactive loop (old C2) appending revisions between items. It was
replaced before any code existed, on Filip's clarified vision: steering
belongs in the ideation conversation and at the go-gate, not mid-build,
and the builder should have a coding agent's freedom — todo list, tools,
verify-and-fix — rather than a pre-approved item list. What the old
design got right was absorbed: its validation story became C1's
tool/precondition validation, its budgets became D5, its
persisted-artifact discipline became the todo and brief artifacts, and
its default-plan-parity guarantee became the untouched classic mode (P5).
`seed_world` stays as A5 left it — with no plan artifact there is nothing
for it to become. The full superseded design is in git history (this
file, before the 2026-07-19 Arc C rewrite).

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
| 8 | B3 dependencies (+ order-pin test first) | M | ✓ landed (0b0cab0) | plan validation |
| 9 | C1 toolbox registry + tools + lints | M–L | ✓ landed (e989488) | the agent's action surface |
| 10 | C2 agent harness + evaluator | L | ✓ landed (275d8b9) | agentic mode v1 |
| 11 | C3 build observer UI | M | ✓ landed (f039a87); live test = Filip's | watching the build |
| 12 | C4 ideation conversation | L | ✓ landed (b73c275); classic entry disabled | the front door |
| 13 | v2a structural surgery toolset | M–L | ✓ landed (51e8c0d) | the agent's structure fix instrument |
| 14 | C5 ideation notes + note verifier + review gate | L | ✓ landed (36a32db…903bca4) | detail survives Go, scoped; the user gets what they asked for |
| 15 | C6 world explorer + classic-system removal | L | ✓ landed (2 commits, 2026-07-19) | the world is a map, not a list; one build system left |
| 16 | Ecstasy Veil P7 fixes (config contract, note threading, excerpt honesty) | S–M | ✓ landed (5174f95, 39380ca) | steering that actually steers; no fabricated findings |
| 17 | v2c checkpoint/revert | M | ✓ landed (60889d7) | the agent's undo — a destructive mistake is one call back |
| 18 | v2d expand_node tool | M | ✓ landed (e7d2b33) | child maps reachable — the wall from both live runs falls |
| 19 | C7a mid-build user messages + brief-edit tools | S–M | ✓ landed 2026-07-20 | a voice into the running build |
| 20 | C7b merged conversational front door (+ the update_brief fix) | L | ✓ landed 2026-07-20 | one flow: chat the world into shape, watch it build, keep talking |

(A5 landed before B1 — the reverse of the original ordering; nothing
depended on the order.) RuntimeHost (`backend.py` half of A1) can ride along
with any item that touches `backend.py`. Every item lands per P8: module
suite by path + root suite green (`git checkout -- test_data` after), pushed
to `main` before the next begins. Arc B changes no behavior (its
verification is the existing per-feature test files plus new unit tests for
the extracted contracts — pass-spec registration, event-stream compat,
dependency checking); the one deliberate exception is B1's decided removal
of the dead legacy endpoints. Arc C items add behavior: C1 gets per-tool
contract tests and lint fixtures, C2 gets mock-driven harness tests plus
one recorded live run (Arc B's verification pattern), C3 is verified in
real Chrome, C4 with a live conversation check; the first agent-built world
gets the same visual map check before pushing.
