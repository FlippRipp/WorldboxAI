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
from wbworldgen.worldgen import mapspace as _mapspace
from wbworldgen.worldgen import pipeline as _pipeline
from wbworldgen.worldgen import start_locations as _start
from wbworldgen.worldgen import templates as _templates
from wbworldgen.worldgen.enrichment import EnrichmentEngine, SiteExpansionEngine, collect_nodes_by_layer
from wbworldgen.worldgen.enrichment import maps_expand as _maps_expand
from wbworldgen.worldgen.enrichment import sites as _sites_mod
from wbworldgen.worldgen.generation import LLMStepGenerator, MapStepGenerator, MockStepGenerator
from wbworldgen.worldgen.hooks import HookRegistry
from wbworldgen.worldgen.persistence import WorldPersistence
from wbworldgen.worldgen.base import USES_MAP
from wbworldgen.worldgen.steps import world_form as _world_form
from wbworldgen.worldgen.fixtures.mock_data import (
    mock_hierarchy_design, mock_layer_design, mock_layer_rules, mock_lore,
    mock_natural_landmarks, mock_rules, mock_society_factions,
    mock_terrain_regions,
)
from wbworldgen.worldgen.types import StepContext

logger = logging.getLogger(__name__)


def scenario_grounding_text(scenario: dict) -> str:
    """Render a linked scenario record (backend.engine.scenario) as the
    grounding text world generation is seeded with.

    The scenario's situation and opening scene are treated as established
    facts: the generated world must contain the places, people and stakes
    they reference, because the story will open there. Never truncated.
    """
    parts = []
    name = str(scenario.get("name") or "").strip()
    if name:
        parts.append(f"Scenario: {name}")
    desc = str(scenario.get("scenario_description") or "").strip()
    if desc:
        parts.append(f"Setting and situation:\n{desc}")
    opening = str(scenario.get("starting_prompt") or "").strip()
    if opening:
        parts.append(
            "The story will open with this exact scene — the world must contain "
            f"the places, people and situation it references:\n{opening}")
    for key, label in (("themes", "Themes"), ("tags", "Tags"), ("pacing", "Pacing")):
        val = str(scenario.get(key) or "").strip()
        if val:
            parts.append(f"{label}: {val}")
    return "\n\n".join(parts)


def build_world_prompt_messages(instruction: str, current_text: str = "",
                                scenario: dict | None = None) -> list[dict]:
    """LLM messages for writing a world SEED PROMPT from the player's notes.

    The player types free-form direction (the enrich field) and optionally has
    a draft prompt and/or a linked scenario; the model turns them into a
    concise seed prompt — the creative direction the generator expands into
    rules, lore and a map, NOT the world itself and NOT in-fiction narration.
    Pure (no I/O) so it is unit-testable; the route feeds the result to the
    LLM. Mirrors the scenario editor's prompt-rewrite framing.
    """
    system = (
        "You are a world-building assistant that writes the SEED PROMPT for an AI "
        "world generator. A seed prompt is a short, vivid paragraph of creative "
        "direction — premise, setting, tone, and any defining features — that the "
        "generator expands into a full world (rules, lore, regions, a map). It is "
        "NOT the world itself and NOT in-fiction narration: write it as direction "
        "for the generator, in plain descriptive prose, a few sentences long. "
        'Return only valid JSON: {"text": "..."}.'
    )
    parts = []
    grounding = scenario_grounding_text(scenario) if scenario else ""
    if grounding:
        parts.append(
            "The world must fit this scenario the player has chosen — honor its "
            "setting, situation, names and tone:\n"
            f"<scenario>\n{grounding}\n</scenario>")
    current_text = (current_text or "").strip()
    if current_text:
        parts.append(f"<current_world_prompt>\n{current_text}\n</current_world_prompt>")
    else:
        parts.append("<current_world_prompt>\n(empty — write a new seed prompt from scratch)\n</current_world_prompt>")
    instr = (instruction or "").strip()
    parts.append(
        "<direction>\n"
        + (instr or "Write a fitting world seed prompt from the scenario above.")
        + "\n</direction>")
    parts.append(
        "Write or revise the world seed prompt to follow the direction, building on "
        "the current prompt when present and grounding everything in the scenario "
        "when one is given. Return only the seed prompt text.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def seed_with_scenario(world_state: dict, user_prompt: str) -> str:
    """The effective seed text for generation: the user's prompt, plus the
    optional scenario document supplied at world creation.

    The scenario is longer-form source material (a campaign setting, an
    adventure premise, pasted background text) the world must be grounded in;
    the seed prompt is the creative direction on top of it. Composed here —
    the single seam every step generation passes through — so the LLM, mock
    and custom-step paths all see both. Never truncated.
    """
    scenario = str((world_state or {}).get("scenario") or "").strip()
    if not scenario:
        return user_prompt
    return (
        f"{user_prompt}\n\n"
        "The world's creator also provided a scenario — source material this world is set in. "
        "Ground the world in it: keep its facts, names, tone and situation consistent, and treat "
        "the seed prompt above as direction for what to build from it.\n"
        "--- SCENARIO ---\n"
        f"{scenario}\n"
        "--- END SCENARIO ---"
    )


class WorldBuilder:
    def __init__(self, worlds_dir: str = "data/worlds"):
        self._steps: dict = {}
        self._ordered_ids: list[str] = []

        self._llm_service = None
        self._settings = None
        self._world_builder_temperature = None
        self._json_retry_attempts = 2

        self._persistence = WorldPersistence(worlds_dir)
        self._worlds_dir = self._persistence._dir
        # Shared references so legacy direct-attribute access keeps working.
        self._enrichment_cache = self._persistence._enrichment_cache
        self._enrichment_cache_max = self._persistence._enrichment_cache_max
        self._enrichment_prompts = self._persistence._enrichment_prompts
        self._enrichment_delay_ms = 300
        # Global ceiling on concurrent enrichment LLM calls (legacy per-node
        # endpoints + batch runs share it). Resized from the
        # world.enrichment_concurrency setting at run start.
        self._enrichment_concurrency = 3
        self._enrichment_batch_size = 8
        self._enrichment_semaphore = asyncio.Semaphore(self._enrichment_concurrency)

        self._hook_registry = HookRegistry()
        self._module_hooks = self._hook_registry.hooks

        self._mock_gen = MockStepGenerator()
        self._map_gen = MapStepGenerator(worlds_dir=str(self._worlds_dir))
        self._llm_gen = LLMStepGenerator(settings=None, retry_attempts=self._json_retry_attempts)
        self._enrichment = EnrichmentEngine(host=self)
        self._sites = SiteExpansionEngine(host=self)
        self._maps_expand = _maps_expand.MapExpansionEngine(host=self)
        self._templates = _templates.load_templates()

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

    def get_pipeline(self, template_id: str = None) -> list[dict]:
        template = self.get_template(template_id)
        skip = set(template.skip_steps)
        return [
            template.apply_to_step(self._steps[sid]).to_frontend()
            for sid in self._ordered_ids if sid not in skip
        ]

    def _build_chain_context(self, world_state: dict, up_to_step_id: str) -> dict:
        return _pipeline.build_chain_context(self._ordered_ids, world_state, up_to_step_id, self._steps)

    # --- world templates ----------------------------------------------------

    def list_templates(self) -> list[dict]:
        return [
            {"id": t.id, "label": t.label, "description": t.description}
            for t in self._templates.values()
        ]

    def get_template(self, template_id: str = None) -> "_templates.WorldTemplate":
        return _templates.get_template(self._templates, template_id)

    def template_for(self, world_state: dict) -> "_templates.WorldTemplate":
        return self.get_template((world_state or {}).get("template_id"))

    def ordered_ids_for(self, world_state: dict) -> list[str]:
        """The effective step order for a world: the registered order minus the
        world's template skip_steps and the skips its own world_form design
        decided (abstract map style, AI-skipped optional steps). `resolve_order`
        itself stays untouched, so `after` chains keep resolving against the
        full registry."""
        skip = set(self.template_for(world_state).skip_steps)
        skip |= _world_form.dynamic_skips(world_state)
        return [sid for sid in self._ordered_ids if sid not in skip]

    # --- generation ---------------------------------------------------------

    async def generate_step(self, step_id: str, world_state: dict, user_prompt: str, user_note: str = "", config: dict = None) -> dict:
        step = self._steps.get(step_id)
        if not step:
            raise ValueError(f"Unknown step: {step_id}")

        user_prompt = seed_with_scenario(world_state, user_prompt)

        # Custom steps may override generation entirely.
        custom = getattr(step, "generate", None)
        if callable(custom):
            ctx = StepContext(step=step, world_state=world_state, user_prompt=user_prompt,
                              user_note=user_note, config=config, services=self)
            data = await custom(ctx)
            await self._hook_registry.dispatch_step(step_id, data, world_state, user_prompt)
            return data

        template = self.template_for(world_state)

        uses = getattr(step, "uses", "llm")
        if step_id == "map_generation" or uses == USES_MAP:
            if step_id == "map_generation" and config is None:
                # No explicit node count: fall back to the template's default
                # scale (a city is denser but smaller than an overworld).
                default_nodes = template.default_total_nodes()
                if default_nodes:
                    config = {"total_nodes": default_nodes}
            # The template's root level picks the map generator (world_map,
            # city_roadnet, ...); non-root levels are generated on expansion.
            # The world's own design (world_form map_style "city") overrides
            # the template default, so "let the AI decide" worlds that read
            # as a city get a real street network.
            levels = template.resolved_levels() or [{}]
            root_gen = (_world_form.map_generator_override(world_state)
                        or levels[0].get("generator_id") or "world_map")
            # Delaunay + road pathfinding are CPU-bound; keep the event loop free.
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, lambda: self._map_gen.generate(world_state, config, root_gen))

        if self._llm_service and self._llm_service.mode != "mock":
            context = self._build_chain_context(world_state, step_id)
            effective = template.apply_to_step(step)
            data = await self._llm_gen.generate(
                effective, context, user_prompt, user_note,
                system_framing=template.resolved_system_framing(),
                coverage_directive=_world_form.coverage_directive(world_state, step_id))
        else:
            data = self._mock_gen.generate(step, world_state, user_prompt, user_note)

        pinned = template.pinned_values.get(step_id)
        if isinstance(pinned, dict) and isinstance(data, dict):
            # Contract keys hidden from the form still land in the output
            # (e.g. sci-fi pins magic_level "none").
            data.update(pinned)

        await self._hook_registry.dispatch_step(step_id, data, world_state, user_prompt)
        return data

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
        user_prompt = seed_with_scenario(world_state, user_prompt)
        template = self.template_for(world_state)
        step = template.apply_to_step(step)
        field_schema = (step.schema or {}).get(field)
        if not isinstance(field_schema, dict):
            raise ValueError(f"Unknown field '{field}' on step '{step_id}'")

        items = list(items or [])
        is_structured = isinstance(field_schema.get("item_schema"), dict)

        if self._llm_service and self._llm_service.mode != "mock":
            context = self._build_chain_context(world_state, step_id)
            framing = template.resolved_system_framing()
            directive = _world_form.coverage_directive(world_state, step_id)
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
        # The template's hierarchy levels (free text) ride into the compiled
        # world so play-time expansion knows which child levels may exist.
        if not world_state.get("hierarchy_levels"):
            world_state = dict(world_state)
            world_state["hierarchy_levels"] = self.template_for(world_state).resolved_levels()
        return compiler.compile_world(world_state, self._steps)

    # --- persistence (delegated) -------------------------------------------

    def list_worlds(self) -> list[dict]:
        return self._persistence.list_worlds()

    def save_world(self, world_id: str, world_state: dict) -> str:
        saved_id = self._persistence.save_world(world_id, world_state)
        self._enrichment.invalidate_compiled(saved_id)
        return saved_id

    def save_draft(self, world_id: str, world_state: dict) -> str:
        saved_id = self._persistence.save_draft(world_id, world_state)
        self._enrichment.invalidate_compiled(saved_id)
        return saved_id

    def load_world(self, world_id: str) -> dict:
        return self._persistence.load_world(world_id)

    def save_step(self, world_id: str, step_id: str, step_data: dict):
        result = self._persistence.save_step(world_id, step_id, step_data)
        self._enrichment.invalidate_compiled(world_id)
        return result

    def delete_world(self, world_id: str):
        result = self._persistence.delete_world(world_id)
        self._enrichment.invalidate_compiled(world_id)
        return result

    def seed_world(self, seed_prompt: str, world_id: str = None, total_nodes: int = 60) -> dict:
        import uuid
        safe_id = world_id or uuid.uuid4().hex[:8]
        safe_id = "".join(c for c in safe_id.lower().replace(" ", "_") if c.isalnum() or c in "_-")

        world_state = {"seed_prompt": seed_prompt, "steps": {}, "complete": False, "current_step": None}
        from wbworldgen.worldgen.fixtures.mock_data import MOCK_GENERATORS

        note_for_layer = ""
        for step_id in self._ordered_ids:
            if step_id == "map_generation":
                data = self._map_gen.generate(world_state, {"total_nodes": total_nodes})
            else:
                handler = MOCK_GENERATORS.get(step_id)
                if handler:
                    data = handler(seed_prompt, note_for_layer)
                else:
                    data = {}
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

    async def enrich_next_label(self, world_id: str, labeled_node_ids: list = None, layer_filter: str = None, rework: bool = False) -> dict:
        return await self._enrichment.label_next(world_id, labeled_node_ids, layer_filter, rework=rework)

    async def enrich_next_description(self, world_id: str, labeled_node_ids: list = None, layer_filter: str = None, rework: bool = False) -> dict:
        return await self._enrichment.describe_next(world_id, labeled_node_ids, layer_filter, rework=rework)

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
                         importance_floor: int = None, node_ids: list = None) -> dict:
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
        return self._enrichment.get_node(world_id, node_id)

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
                          force: bool = False) -> dict:
        """Generate (or return the cached) child map for one anchor node.

        One full-attention LLM call, cached write-once under the world's
        ``maps/`` directory — every save of the world inherits it. Returns
        {"map": MapRecord, "connections": [ConnectionRecord]}.
        """
        if not force:
            existing = self.get_child_map(world_id, map_id, node_id)
            if existing:
                return existing
        compiled = self._enrichment._load_compiled(world_id)
        node = self._enrichment.get_node(world_id, node_id)
        if node is None:
            raise ValueError(f"Unknown map node: {node_id}")
        max_locations = self._resolve_enrichment_setting("world.site_max_sublocations", 10, 4, 16)
        bundle = await self._maps_expand.expand(
            compiled, map_id, node, max_locations=max_locations,
            template_vocab=compiled.get("template_vocab"))
        self._persistence.save_child_map(world_id, bundle)
        # Keep the cached compiled world truthful without a full invalidation
        # (a reload would also re-read maps/ via load_world).
        record = bundle["map"]
        compiled.setdefault("maps", {})[record["map_id"]] = record
        compiled.setdefault("connections", []).extend(bundle["connections"])
        compiled.pop("_node_by_id", None)
        return bundle

    async def pregenerate_planned_maps(self, world_id: str, on_event=None) -> dict:
        """Build the child maps hierarchy_design planned for upfront creation
        (seed-central locations). One full-attention call per map, serialized.
        Unmatched names are skipped with a warning — they expand lazily later."""
        world_state = self.load_world(world_id)
        planned = (world_state.get("steps", {}).get("hierarchy_design", {})
                   .get("data", {}) or {}).get("pregenerate") or []
        summary = {"built": [], "skipped": []}
        if not planned:
            return summary
        compiled = self._enrichment._load_compiled(world_id)
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
                bundle = await self.expand_node(world_id, map_id, node.get("id"))
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

    async def llm_pick_start_location(self, world_id: str, preference: str, llm):
        """Pick the best existing start candidate — or, when nothing genuinely
        matches the player's request, author a brand-new start location on one
        of the world's unnamed map positions and persist it into the world."""
        compiled = self.compile_world(self.load_world(world_id))
        candidates = _start.get_start_locations(compiled)
        live = llm is not None and getattr(llm, "mode", "mock") != "mock"
        result = await _start.llm_pick_start_location(
            compiled, candidates, preference, llm, allow_no_match=live)
        if not (isinstance(result, dict) and result.get("no_match")):
            return result

        authored = await _start.generate_start_location(
            compiled, preference, result.get("wanted", ""), llm)
        if not authored:
            # Generation failed — settle for the best existing candidate.
            return await _start.llm_pick_start_location(compiled, candidates, preference, llm)
        return self._persist_generated_start(world_id, authored)

    async def author_location(self, world_id: str, description: str) -> dict | None:
        """Author a brand-new named location matching a free-text description
        onto one of the world's unnamed map positions (one full-attention
        call) — used when the story needs a place that doesn't exist yet
        (e.g. a teleport to a named-but-unmapped destination). Returns the
        candidate-shaped entry (node_id, map_id, ...) or None when no slot
        fits or the call fails."""
        if not self._llm_service or self._llm_service.mode == "mock":
            return None
        compiled = self._enrichment._load_compiled(world_id)
        try:
            authored = await _start.generate_start_location(
                compiled, description, description, self._llm_service)
        except Exception:
            logger.exception("on-demand location authoring failed")
            return None
        if not authored:
            return None
        result = self._persist_generated_start(world_id, authored)
        self._enrichment.invalidate_compiled(world_id)
        return result

    def _persist_generated_start(self, world_id: str, authored: dict) -> dict:
        """Write an on-demand start location onto its map node (name, type,
        description, importance bump) and return it in candidate shape."""
        node_id = authored["node_id"]
        writes = {
            "name": authored["name"],
            "type": authored["type"],
            "importance": self.MAJOR_IMPORTANCE_FLOOR if authored["type"] == "landmark" else 8,
        }
        if authored.get("label_description"):
            writes["label_description"] = authored["label_description"]
        if authored.get("description"):
            writes["description"] = authored["description"]
        for field, value in writes.items():
            self._save_node_enrichment(world_id, node_id, field, value)
        self._flush_enrichment_cache(world_id)
        self._enrichment.invalidate_compiled(world_id)

        compiled = self.compile_world(self.load_world(world_id))
        for entry in _start.get_start_locations(compiled):
            if entry.get("node_id") == node_id:
                entry["reason"] = authored.get("reason", "")
                entry["generated"] = True
                return entry
        # Node fell outside the candidate filter (shouldn't happen) — return
        # the authored fields directly so the caller still gets a start.
        return {**authored, "generated": True}

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
