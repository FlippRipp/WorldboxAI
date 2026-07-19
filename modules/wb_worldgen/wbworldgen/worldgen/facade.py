"""WorldBuilder facade.

Composition root that wires the modular world-generation components
(generators, persistence, hooks, enrichment) together and exposes the stable
public interface relied on by the API layer, CharacterBuilder and tests.

The heavy logic lives in the focused sibling modules; this facade is a thin
coordinator that holds the small amount of orchestration state.
"""

import asyncio
import logging

from wbworldgen.worldgen import compiler
from wbworldgen.worldgen import design as _design
from wbworldgen.worldgen import mapspace as _mapspace
from wbworldgen.worldgen import pipeline as _pipeline
from wbworldgen.worldgen import start_locations as _start
from wbworldgen.worldgen.compiled_cache import CompiledWorldCache
from wbworldgen.worldgen.generation.llm import DEFAULT_SYSTEM_FRAMING
from wbworldgen.worldgen.enrichment import EnrichmentEngine, collect_nodes_by_layer
from wbworldgen.worldgen.expansion import SiteExpansionEngine
from wbworldgen.worldgen.expansion import maps_expand as _maps_expand
from wbworldgen.worldgen.expansion import sites as _sites_mod
from wbworldgen.worldgen.generation import LLMStepGenerator, MapStepGenerator, MockStepGenerator
from wbworldgen.worldgen.hooks import HookRegistry
from wbworldgen.worldgen.persistence import WorldPersistence
from wbworldgen.worldgen.services import GenServices
from wbworldgen.worldgen.base import USES_ENRICHMENT, USES_MAP
from wbworldgen.worldgen.fixtures.mock_data import (
    mock_hierarchy_design, mock_layer_design, mock_layer_rules, mock_lore,
    mock_natural_landmarks, mock_rules, mock_society_factions,
    mock_terrain_regions,
)
from wbworldgen.worldgen.types import StepContext

logger = logging.getLogger(__name__)

# Re-exported for compatibility: routes, tests and older callers import
# these from the facade; they live in prompts.py now.
from wbworldgen.worldgen.prompts import (  # noqa: F401
    build_world_prompt_messages,
    scenario_grounding_text,
    scenario_start_brief,
    seed_with_scenario,
)



class WorldBuilder:
    def __init__(self, worlds_dir: str = "data/worlds"):
        self._steps: dict = {}
        self._ordered_ids: list[str] = []
        self._settings = None

        self._persistence = WorldPersistence(worlds_dir)
        self._worlds_dir = self._persistence._dir
        # Shared references so legacy direct-attribute access keeps working.
        self._enrichment_cache = self._persistence._enrichment_cache
        self._enrichment_cache_max = self._persistence._enrichment_cache_max
        self._enrichment_prompts = self._persistence._enrichment_prompts
        self._enrichment_delay_ms = 300
        # Global ceiling on concurrent enrichment LLM calls. Resized from the
        # world.enrichment_concurrency setting at run start.
        self._enrichment_concurrency = 3
        self._enrichment_batch_size = 8

        # The explicit engine contract: every dependency the engines read is
        # a named field on GenServices (facade privates delegate below, so
        # legacy attribute access keeps working and engines see every write).
        # load_world resolves late so callers that rebind the facade's loader
        # are seen by cached compiled loads too.
        self._compiled = CompiledWorldCache(
            load_world=lambda world_id: self.load_world(world_id),
            steps=self._steps,
            terrain_store=self._persistence,
        )
        self._services = GenServices(
            prompts=self._get_prompt,
            enrichment_store=self._persistence,
            compiled=self._compiled,
            load_world=lambda world_id: self.load_world(world_id),
            terrain_store=self._persistence,
            resolve_setting=self._resolve_enrichment_setting,
            semaphore=asyncio.Semaphore(self._enrichment_concurrency),
        )

        self._hook_registry = HookRegistry()
        self._module_hooks = self._hook_registry.hooks

        self._mock_gen = MockStepGenerator()
        self._map_gen = MapStepGenerator(worlds_dir=str(self._worlds_dir))
        self._llm_gen = LLMStepGenerator(settings=None, retry_attempts=self._json_retry_attempts)
        self._enrichment = EnrichmentEngine(self._services)
        self._sites = SiteExpansionEngine(self._services)
        self._maps_expand = _maps_expand.MapExpansionEngine(self._services)

    # --- engine-shared config -----------------------------------------------
    # GenServices is the single source of truth; these properties keep the
    # legacy private names readable AND assignable (tests set them directly)
    # while the engines see every write.

    @property
    def _llm_service(self):
        return self._services.llm

    @_llm_service.setter
    def _llm_service(self, service):
        self._services.llm = service

    @property
    def _world_builder_temperature(self):
        return self._services.temperature

    @_world_builder_temperature.setter
    def _world_builder_temperature(self, temperature):
        self._services.temperature = temperature

    @property
    def _json_retry_attempts(self):
        return self._services.json_retry_attempts

    @_json_retry_attempts.setter
    def _json_retry_attempts(self, attempts):
        self._services.json_retry_attempts = attempts

    @property
    def _enrichment_semaphore(self):
        return self._services.semaphore

    @_enrichment_semaphore.setter
    def _enrichment_semaphore(self, semaphore):
        self._services.semaphore = semaphore

    @property
    def services(self) -> GenServices:
        """The explicit engine contract (see ``GenServices``). Public so the
        subsystems that orchestrate through the facade (start_locations, and
        later the plan executor) read dependencies through one named object
        instead of facade privates."""
        return self._services

    # --- configuration ------------------------------------------------------

    def set_llm_service(self, service):
        self._llm_service = service
        self._llm_gen._llm = service

    def set_settings(self, settings):
        self._settings = settings
        self._llm_gen._settings = settings

    def set_world_builder_temperature(self, temperature: float):
        self._world_builder_temperature = temperature
        self._llm_gen._temperature = temperature

    def register_module_hooks(self, registry):
        self._hook_registry.register_from_modules(registry)

    # --- pipeline registration / ordering ----------------------------------

    def register_step(self, step):
        if step.id in self._steps:
            raise ValueError(f"Step {step.id} is already registered.")
        self._steps[step.id] = step
        self._ordered_ids = self._resolve_order()

    def _resolve_order(self) -> list[str]:
        return _pipeline.resolve_order(self._steps)

    def get_pipeline(self) -> list[dict]:
        return [self._steps[sid].to_frontend() for sid in self._ordered_ids]

    def steps_by_id(self) -> dict:
        """The registered step instances, id → step, in registration order
        (module-contributed steps included). A copy — the registry itself is
        only mutated through ``register_step``. Public so the agent toolbox
        (C1) resolves and describes steps without reaching into privates."""
        return dict(self._steps)

    def _build_chain_context(self, world_state: dict, up_to_step_id: str) -> dict:
        return _pipeline.build_chain_context(self._ordered_ids, world_state, up_to_step_id, self._steps)

    def ordered_ids_for(self, world_state: dict) -> list[str]:
        """The effective step order for a world: the registered order minus
        the skips its own world_form design decided (abstract map style,
        AI-skipped optional steps). `resolve_order` itself stays untouched, so
        `after` chains keep resolving against the full registry."""
        skip = _design.dynamic_skips(world_state)
        return [sid for sid in self._ordered_ids if sid not in skip]

    def system_framing_for(self, world_state: dict) -> str:
        """The per-world system framing: the neutral default plus the world's
        own design (world_form's world_kind) as the genre voice. Worlds
        without a design (old worlds, the world_form step itself) keep the
        historical default framing byte-identical."""
        kind = _design.world_kind(world_state)
        if kind:
            return f"{DEFAULT_SYSTEM_FRAMING} This world: {kind}"
        return DEFAULT_SYSTEM_FRAMING

    # --- generation ---------------------------------------------------------

    async def generate_step(self, step_id: str, world_state: dict, user_prompt: str, user_note: str = "", config: dict = None,
                            force_mock: bool = False) -> dict:
        step = self._steps.get(step_id)
        if not step:
            raise ValueError(f"Unknown step: {step_id}")

        user_prompt = seed_with_scenario(world_state, user_prompt)

        # Custom steps may override generation entirely.
        custom = getattr(step, "generate", None)
        if callable(custom):
            ctx = StepContext(step=step, world_state=world_state, user_prompt=user_prompt,
                              user_note=user_note, config=config, services=self,
                              force_mock=force_mock)
            data = await custom(ctx)
            await self._hook_registry.dispatch_step(step_id, data, world_state, user_prompt)
            return data

        uses = getattr(step, "uses", "llm")
        if step_id == "map_generation" or uses == USES_MAP:
            root_gen = _design.root_generator_for(world_state)
            level = _design.authored_root_level(world_state, root_gen)
            if level is not None:
                # Root-as-first-expansion: the whole world is one authored
                # place (an interior-style root level) — the same authored
                # flow expansion uses, minus a parent to anchor to.
                max_locations = self._resolve_enrichment_setting(
                    "world.site_max_sublocations", 10, 4, 16)
                return await self._maps_expand.expand_root(
                    world_state, user_prompt, level,
                    max_locations=max(8, max_locations), force_mock=force_mock,
                    user_note=user_note)
            abstract_level = _design.abstract_root_level(world_state, root_gen)
            if abstract_level is not None:
                # Abstract worlds (a solar system, a dream web) get an
                # AUTHORED root graph: real named places from the hierarchy
                # guidance and the world design's map directive, instead of
                # a procedural scatter that ignores both.
                return await self._maps_expand.expand_abstract_root(
                    world_state, user_prompt, abstract_level,
                    directive=_design.coverage_directive(world_state, "map_generation"),
                    world_kind=_design.world_kind(world_state),
                    force_mock=force_mock, user_note=user_note)
            # Delaunay + road pathfinding are CPU-bound; keep the event loop free.
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, lambda: self._map_gen.generate(world_state, config, root_gen))

        data = await self.generate_declarative(step, world_state, user_prompt,
                                               user_note, force_mock=force_mock)
        await self._hook_registry.dispatch_step(step_id, data, world_state, user_prompt)
        return data

    async def generate_declarative(self, step, world_state: dict, user_prompt: str,
                                   user_note: str = "", force_mock: bool = False) -> dict:
        """The declarative generation body: the step's schema+guidance prompt
        over the chain context on the live LLM, or the mock generator
        offline. Public so a custom ``generate(ctx)`` override can compose
        it — run the normal generation, then post-process — instead of
        duplicating the path (precedent: world_rules' brief-input handling,
        C4)."""
        # Duck-typed steps (tests, legacy) may predate the per-world view hook.
        view = getattr(step, "view_for", None)
        if callable(view):
            step = view(world_state)
        if not force_mock and self._llm_service and self._llm_service.mode != "mock":
            context = self._build_chain_context(world_state, step.id)
            return await self._llm_gen.generate(
                step, context, user_prompt, user_note,
                system_framing=self.system_framing_for(world_state),
                coverage_directive=_design.coverage_directive(world_state, step.id))
        return self._mock_gen.generate(step, world_state, user_prompt, user_note)

    async def regenerate_list_item(
        self, step_id: str, field: str, items: list, index: int,
        world_state: dict, user_prompt: str, user_note: str = "",
        subfield: str = None,
    ):
        """Regenerate a single entry of a step's list field.

        For string-list fields this returns a string. For structured (object)
        list fields it returns a dict (the whole entry), or — when ``subfield``
        is given — just the new value for that sub-field. Stateless: the caller
        splices the result into the field value and persists it.
        """
        step = self._steps.get(step_id)
        if not step:
            raise ValueError(f"Unknown step: {step_id}")
        view = getattr(step, "view_for", None)
        if callable(view):
            step = view(world_state)
        user_prompt = seed_with_scenario(world_state, user_prompt)
        field_schema = (step.schema or {}).get(field)
        if not isinstance(field_schema, dict):
            raise ValueError(f"Unknown field '{field}' on step '{step_id}'")

        items = list(items or [])
        is_structured = isinstance(field_schema.get("item_schema"), dict)

        if self._llm_service and self._llm_service.mode != "mock":
            context = self._build_chain_context(world_state, step_id)
            framing = self.system_framing_for(world_state)
            directive = _design.coverage_directive(world_state, step_id)
            if is_structured:
                return await self._llm_gen.generate_structured_item(
                    step, field, field_schema, items, index, context, user_prompt,
                    user_note, subfield=subfield, system_framing=framing,
                    coverage_directive=directive,
                )
            return await self._llm_gen.generate_list_item(
                step, field, field_schema, items, index, context, user_prompt, user_note,
                system_framing=framing, coverage_directive=directive,
            )

        # Mock fallback: deterministic, distinct from existing entries.
        if is_structured:
            return self._mock_structured_item(field_schema, items, index, subfield)

        label = field_schema.get("label", field)
        existing = {str(it).strip().lower() for it in items}
        n = len(items)
        while True:
            candidate = f"A freshly conjured {label.lower()} (variant {n + 1})"
            if candidate.lower() not in existing:
                return candidate
            n += 1

    @staticmethod
    def _mock_structured_item(field_schema: dict, items: list, index: int, subfield: str = None):
        """Deterministic mock for a structured list entry (or one sub-field)."""
        item_schema = field_schema.get("item_schema", {})
        current = items[index] if 0 <= index < len(items) else {}
        n = len(items) + 1

        def mock_value(key, sub):
            sub_type = sub.get("type") if isinstance(sub, dict) else sub
            label = (sub.get("label", key) if isinstance(sub, dict) else key).lower()
            if sub_type == "list":
                return [f"mock {label} (variant {n})"]
            if sub_type == "select" and isinstance(sub, dict) and sub.get("options"):
                return sub["options"][0]
            return f"Mock {label} (variant {n})"

        if subfield:
            return mock_value(subfield, item_schema.get(subfield, {}))

        new_item = dict(current) if isinstance(current, dict) else {}
        for key, sub in item_schema.items():
            new_item[key] = mock_value(key, sub)
        return new_item

    # --- compilation --------------------------------------------------------

    def _merge_geography_steps(self, steps_data: dict) -> dict:
        return compiler.merge_geography_steps(steps_data)

    def compile_world(self, world_state: dict) -> dict:
        # Hierarchy levels resolve inside the pure compiler (the world's own
        # AI-designed structure, default [world, interior] when absent) so
        # the facade and the enrichment engine always agree.
        return compiler.compile_world(world_state, self._steps)

    # --- persistence (delegated) -------------------------------------------

    def list_worlds(self) -> list[dict]:
        return self._persistence.list_worlds()

    def save_world(self, world_id: str, world_state: dict) -> str:
        saved_id = self._persistence.save_world(world_id, world_state)
        self._compiled.invalidate(saved_id)
        return saved_id

    def save_draft(self, world_id: str, world_state: dict) -> str:
        saved_id = self._persistence.save_draft(world_id, world_state)
        self._compiled.invalidate(saved_id)
        return saved_id

    def load_world(self, world_id: str) -> dict:
        return self._persistence.load_world(world_id)

    def save_step(self, world_id: str, step_id: str, step_data: dict):
        result = self._persistence.save_step(world_id, step_id, step_data)
        self._compiled.invalidate(world_id)
        return result

    def delete_world(self, world_id: str):
        result = self._persistence.delete_world(world_id)
        self._compiled.invalidate(world_id)
        return result

    async def seed_world(self, seed_prompt: str, world_id: str = None, total_nodes: int = 60) -> dict:
        """Build a complete world offline: the normal pipeline step by step
        with the mock strategy forced (a live LLM is never called), every
        step approved. Terrain rasters and the engine-driven enrichment
        steps stay empty — a seeded world is a pre-enrichment draft, exactly
        like a wizard world before the enrichment panel runs."""
        import uuid
        safe_id = world_id or uuid.uuid4().hex[:8]
        safe_id = "".join(c for c in safe_id.lower().replace(" ", "_") if c.isalnum() or c in "_-")

        world_state = {"seed_prompt": seed_prompt, "steps": {}, "complete": False, "current_step": None}

        note_for_layer = ""
        for step_id in self._ordered_ids:
            step = self._steps[step_id]
            if step_id == "terrain_generation" or getattr(step, "uses", "") == USES_ENRICHMENT:
                data = {}
            else:
                data = await self.generate_step(
                    step_id, world_state, seed_prompt, user_note=note_for_layer,
                    config={"total_nodes": total_nodes}, force_mock=True)
            world_state["steps"][step_id] = {"data": data, "approved": True}
            if step_id == "hierarchy_design" and isinstance(data, dict) and data.get("parallel_maps"):
                note_for_layer = "world with parallel maps"

        world_id = self.save_world(safe_id, world_state)
        compiled = self.compile_world(world_state)
        return {
            "world_id": world_id,
            "seed_prompt": seed_prompt,
            "step_count": len(world_state["steps"]),
            "compiled_keys": list(compiled.keys()),
            "total_map_nodes": len(_mapspace.all_nodes(compiled)),
        }

    # --- enrichment cache (delegated) --------------------------------------

    def _save_node_enrichment(self, world_id: str, node_id: str, field: str, value: str):
        return self._persistence.save_node_enrichment(world_id, node_id, field, value)

    def _flush_enrichment_cache(self, world_id: str = None):
        return self._persistence.flush_enrichment_cache(world_id)

    def _write_enrichment_to_disk(self, world_id: str):
        return self._persistence.write_enrichment_to_disk(world_id)

    def _build_enrichment_node_index(self, map_data: dict) -> dict:
        return self._persistence.build_enrichment_node_index(map_data)

    def sync_enrichment_to_map_state(self, map_data: dict, node_map: dict):
        return self._persistence.sync_enrichment_to_map_state(map_data, node_map)

    def _load_enrichment_prompts(self):
        return self._persistence.load_enrichment_prompts()

    def _get_prompt(self, prompt_id: str, fallback: str, **kwargs) -> str:
        return self._persistence.get_prompt(prompt_id, fallback, **kwargs)

    def _collect_nodes_by_layer(self, compiled: dict, layer_filter: str = None) -> tuple:
        return collect_nodes_by_layer(compiled, layer_filter)

    # --- enrichment generation (delegated) ---------------------------------

    def _resolve_enrichment_setting(self, key: str, current: int, lo: int, hi: int) -> int:
        """Live-read an integer enrichment setting; clamp to a sane range."""
        value = current
        if self._settings is not None:
            try:
                configured = self._settings.get(key)
                if configured is not None:
                    value = int(configured)
            except Exception:
                pass
        return max(lo, min(value, hi))

    async def enrich_run(self, world_id: str, phase: str = "all", count: int = None,
                         layer_filter: str = None, rework: bool = False,
                         exclude_node_ids: list = None, on_event=None,
                         importance_floor: int = None, node_ids: list = None,
                         guidance: str = None, spec=None) -> dict:
        """``guidance`` is the run-level steering note threaded to every pass
        (C1's guidance channel); ``spec`` runs one explicit ``PassSpec``
        instead of resolving ``phase`` — the ad-hoc `pass:custom` path."""
        # 1 keeps the old fully-serialized behavior for rate-limited providers.
        concurrency = self._resolve_enrichment_setting(
            "world.enrichment_concurrency", self._enrichment_concurrency, 1, 8)
        # Batch size 1 disables batched labeling (one node per LLM call).
        batch_size = self._resolve_enrichment_setting(
            "world.enrichment_batch_size", self._enrichment_batch_size, 1, 10)
        self._enrichment_batch_size = batch_size
        if concurrency != self._enrichment_concurrency:
            # Swap in a right-sized semaphore; in-flight holders release on the
            # old object they acquired, so resizing mid-flight is safe.
            self._enrichment_concurrency = concurrency
            self._enrichment_semaphore = asyncio.Semaphore(concurrency)
        return await self._enrichment.run(
            world_id, phase=phase, count=count, layer_filter=layer_filter,
            rework=rework, exclude_node_ids=exclude_node_ids,
            concurrency=concurrency, batch_size=batch_size, on_event=on_event,
            importance_floor=importance_floor, node_ids=node_ids,
            guidance=guidance, specs=[spec] if spec is not None else None,
        )

    def enrich_cancel(self, world_id: str):
        self._enrichment.cancel(world_id)

    # Importance at or above this marks a node as a "major location": authored
    # settlements bind at importance 8 and landmarks at 6 (world_map anchoring),
    # so 6 covers every authored named_location plus high-degree hubs.
    MAJOR_IMPORTANCE_FLOOR = 6

    def default_importance_floor(self) -> int | None:
        """The upfront-enrichment importance floor from the
        ``world.upfront_detail`` setting: ``major_locations`` (the default)
        details only major nodes at world creation — the rest is generated
        on demand during play; ``full`` restores enrich-everything."""
        detail = "major_locations"
        if self._settings is not None:
            try:
                configured = self._settings.get("world.upfront_detail")
                if configured:
                    detail = str(configured)
            except Exception:
                pass
        return None if detail == "full" else self.MAJOR_IMPORTANCE_FLOOR

    def get_map_node(self, world_id: str, node_id: str) -> dict | None:
        """Current state of one map node (enrichment fields included)."""
        return self._compiled.get_node(world_id, node_id)

    async def detail_nodes(self, world_id: str, node_ids: list) -> dict:
        """Label + describe an explicit set of nodes (play-time backfill).
        Same single-call-per-node quality as the upfront pass."""
        return await self.enrich_run(world_id, phase="all", node_ids=list(node_ids))

    # --- child-map expansion (lazy interiors as real maps) ------------------

    def is_node_expandable(self, compiled: dict, map_id: str, node: dict) -> bool:
        return _maps_expand.is_expandable(compiled, map_id, node)

    def get_child_map(self, world_id: str, parent_map_id: str, node_id: str) -> dict | None:
        """Cached child-map bundle {"map", "connections"} for an anchor, if any."""
        return self._persistence.load_child_map(
            world_id, _maps_expand.child_map_id(parent_map_id, node_id))

    async def expand_node(self, world_id: str, map_id: str, node_id: str,
                          force: bool = False, level_type: str = None,
                          must_include: str = None) -> dict:
        """Generate (or return the cached) child map for one anchor node.

        One full-attention LLM call, cached under the world's ``maps/``
        directory — every save of the world inherits it. Returns
        {"map": MapRecord, "connections": [ConnectionRecord]}. ``level_type``
        pins the child's level (pregenerate plans, explicit caller choice);
        otherwise the LLM picks from the allowed levels.
        """
        if not force:
            existing = self.get_child_map(world_id, map_id, node_id)
            if existing:
                return existing
        compiled = self._compiled.load(world_id)
        node = self._compiled.get_node(world_id, node_id)
        if node is None:
            raise ValueError(f"Unknown map node: {node_id}")
        max_locations = self._resolve_enrichment_setting("world.site_max_sublocations", 10, 4, 16)
        child_nodes = self._resolve_enrichment_setting("world.child_map_nodes", 60, 20, 200)
        bundle = await self._maps_expand.expand(
            compiled, map_id, node, max_locations=max_locations,
            template_vocab=compiled.get("template_vocab"),
            level_type=level_type, total_nodes=child_nodes, world_id=world_id,
            must_include=must_include)
        self._persistence.save_child_map(world_id, bundle)
        # Keep the cached compiled world truthful without a full invalidation
        # (a reload would also re-read maps/ via load_world).
        record = bundle["map"]
        compiled.setdefault("maps", {})[record["map_id"]] = record
        compiled.setdefault("connections", []).extend(bundle["connections"])
        compiled.pop("_node_by_id", None)
        # A terrain-flagged child brings its own rasters — drop the attached
        # terrain cache so enrichment re-loads with the new map included.
        if (record.get("config") or {}).get("terrain"):
            compiled.pop("_terrain_layers", None)
        return bundle

    async def grow_child_map(self, world_id: str, map_id: str, description: str,
                             near_node_id: str = None) -> dict | None:
        """Author ONE new location onto an existing child map — the story
        walked somewhere inside a site that isn't on its map yet ("the
        storage building behind the school"). ``near_node_id`` (the player's
        current node) anchors the placement and the fallback adjacency.

        Returns ``{"node", "edges", "created"}`` — ``created`` is False when
        the request matched an already-existing location — or the engine's
        ``{"belongs_outside": True}`` veto (the place is its own destination
        in the wider world; the caller authors it outside instead), or None
        when the map isn't a persisted child map or authoring failed."""
        bundle = self._persistence.load_child_map(world_id, map_id)
        if not bundle:
            return None
        compiled = self._compiled.load(world_id)
        try:
            grown = await self._maps_expand.grow(
                compiled, bundle["map"], description, near_node_id=near_node_id,
                template_vocab=compiled.get("template_vocab"))
        except Exception:
            logger.exception("child-map grow failed for %s/%s", world_id, map_id)
            return None
        if not grown:
            return None
        if grown.get("created"):
            self._persistence.save_child_map(world_id, bundle)
            # The bundle's record is a fresh read, not the cached compiled's —
            # drop the cache so the next load sees the grown map.
            self._compiled.invalidate(world_id)
        return grown

    async def pregenerate_planned_maps(self, world_id: str, on_event=None) -> dict:
        """Build the child maps hierarchy_design planned for upfront creation
        (seed-central locations). One full-attention call per map, serialized.
        Unmatched names are skipped with a warning — they expand lazily later."""
        world_state = self.load_world(world_id)
        planned = _design.pregenerate_plans(world_state)
        summary = {"built": [], "skipped": []}
        if not planned:
            return summary
        compiled = self._compiled.load(world_id)
        from wbworldgen.worldgen import mapspace as _ms
        by_name = {}
        for mid, m in _ms.maps_by_id(compiled).items():
            for n in m.get("nodes", []):
                if n.get("name"):
                    by_name.setdefault(n["name"].strip().lower(), (mid, n))
        for entry in planned:
            name = str((entry or {}).get("location_name", "")).strip()
            hit = by_name.get(name.lower()) if name else None
            if hit is None:
                logger.warning("pregenerate: no named node matches %r; it will expand lazily", name)
                summary["skipped"].append(name)
                continue
            map_id, node = hit
            if not _maps_expand.is_expandable(compiled, map_id, node):
                summary["skipped"].append(name)
                continue
            try:
                bundle = await self.expand_node(
                    world_id, map_id, node.get("id"),
                    level_type=str((entry or {}).get("level_type", "")).strip() or None)
            except Exception as e:
                logger.warning("pregenerate failed for %r: %s", name, e)
                summary["skipped"].append(name)
                continue
            summary["built"].append(bundle["map"]["map_id"])
            if on_event is not None:
                await on_event({"type": "pregenerated", "name": name,
                                "map_id": bundle["map"]["map_id"]})
        return summary

    # --- deprecated site bundles (superseded by expand_node; kept for the
    # migration read path and old callers) ----------------------------------

    def is_site_expandable(self, node: dict) -> bool:
        return _sites_mod.is_expandable(node)

    def get_site(self, world_id: str, node_id: str) -> dict | None:
        return self._persistence.load_site(world_id, node_id)

    # --- start locations ----------------------------------------------------

    def get_start_locations(self, world_id: str) -> list[dict]:
        compiled = self.compile_world(self.load_world(world_id))
        return _start.get_start_locations(compiled)

    def find_start_location(self, world_id: str, node_id: str) -> dict | None:
        """Candidate-shaped entry for an explicitly pre-picked start node id,
        wherever it lives (including interior maps)."""
        compiled = self.compile_world(self.load_world(world_id))
        return _start.find_start_candidate(compiled, node_id)

    async def llm_pick_start_location(self, world_id: str, preference: str, llm):
        """Choose where the story starts, descending the map hierarchy — the
        full contract lives on ``start_locations.pick_start_location``."""
        return await _start.pick_start_location(self, world_id, preference, llm)

    async def author_location(self, world_id: str, description: str,
                              anchor_node_id: str = None) -> dict | None:
        """Author a brand-new named location for a free-text description —
        see ``start_locations.author_location``."""
        return await _start.author_location(self, world_id, description,
                                            anchor_node_id=anchor_node_id)

    # --- mock fixtures (kept as methods for direct callers/tests) ----------

    def _mock_rules(self, prompt: str, note: str = "") -> dict:
        return mock_rules(prompt, note)

    def _mock_lore(self, prompt: str, note: str = "") -> dict:
        return mock_lore(prompt, note)

    def _mock_hierarchy_design(self, prompt: str, note: str = "") -> dict:
        return mock_hierarchy_design(prompt, note)

    def _mock_layer_design(self, prompt: str, note: str = "") -> dict:
        return mock_layer_design(prompt, note)

    def _mock_layer_rules(self, prompt: str, note: str = "") -> dict:
        return mock_layer_rules(prompt, note)

    def _mock_terrain_regions(self, prompt: str, note: str = "") -> dict:
        return mock_terrain_regions(prompt, note)

    def _mock_natural_landmarks(self, prompt: str, note: str = "") -> dict:
        return mock_natural_landmarks(prompt, note)

    def _mock_society_factions(self, prompt: str, note: str = "") -> dict:
        return mock_society_factions(prompt, note)
