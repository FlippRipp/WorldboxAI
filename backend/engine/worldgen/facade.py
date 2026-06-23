"""WorldBuilder facade.

Composition root that wires the modular world-generation components
(generators, persistence, hooks, enrichment) together and exposes the stable
public interface relied on by the API layer, CharacterBuilder and tests.

The heavy logic lives in the focused sibling modules; this facade is a thin
coordinator that holds the small amount of orchestration state.
"""

import asyncio
import logging

from backend.engine.worldgen import compiler
from backend.engine.worldgen import pipeline as _pipeline
from backend.engine.worldgen import start_locations as _start
from backend.engine.worldgen.enrichment import EnrichmentEngine, collect_nodes_by_layer
from backend.engine.worldgen.generation import LLMStepGenerator, MapStepGenerator, MockStepGenerator
from backend.engine.worldgen.hooks import HookRegistry
from backend.engine.worldgen.persistence import WorldPersistence
from backend.engine.worldgen.base import USES_MAP
from backend.engine.worldgen.fixtures.mock_data import (
    mock_layer_design, mock_layer_rules, mock_lore, mock_natural_landmarks,
    mock_rules, mock_society_factions, mock_terrain_regions,
)
from backend.engine.worldgen.types import StepContext

logger = logging.getLogger(__name__)


class WorldBuilder:
    def __init__(self, worlds_dir: str = "data/worlds"):
        self._steps: dict = {}
        self._ordered_ids: list[str] = []

        self._llm_service = None
        self._settings = None
        self._world_builder_model = None
        self._world_builder_temperature = None
        self._enrich_label_model = None
        self._json_retry_attempts = 2

        self._persistence = WorldPersistence(worlds_dir)
        self._worlds_dir = self._persistence._dir
        # Shared references so legacy direct-attribute access keeps working.
        self._enrichment_cache = self._persistence._enrichment_cache
        self._enrichment_cache_max = self._persistence._enrichment_cache_max
        self._enrichment_prompts = self._persistence._enrichment_prompts
        self._enrichment_delay_ms = 300
        self._enrichment_semaphore = asyncio.Semaphore(1)

        self._hook_registry = HookRegistry()
        self._module_hooks = self._hook_registry.hooks

        self._mock_gen = MockStepGenerator()
        self._map_gen = MapStepGenerator()
        self._llm_gen = LLMStepGenerator(settings=None, retry_attempts=self._json_retry_attempts)
        self._enrichment = EnrichmentEngine(host=self)

    # --- configuration ------------------------------------------------------

    def set_llm_service(self, service):
        self._llm_service = service
        self._llm_gen._llm = service

    def set_settings(self, settings):
        self._settings = settings
        self._llm_gen._settings = settings

    def set_world_builder_model(self, model: str):
        self._world_builder_model = model
        self._llm_gen._model = model

    def set_world_builder_temperature(self, temperature: float):
        self._world_builder_temperature = temperature
        self._llm_gen._temperature = temperature

    def set_enrich_label_model(self, model: str):
        self._enrich_label_model = model

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

    def _build_chain_context(self, world_state: dict, up_to_step_id: str) -> dict:
        return _pipeline.build_chain_context(self._ordered_ids, world_state, up_to_step_id)

    # --- generation ---------------------------------------------------------

    async def generate_step(self, step_id: str, world_state: dict, user_prompt: str, user_note: str = "", config: dict = None) -> dict:
        step = self._steps.get(step_id)
        if not step:
            raise ValueError(f"Unknown step: {step_id}")

        # Custom steps may override generation entirely.
        custom = getattr(step, "generate", None)
        if callable(custom):
            ctx = StepContext(step=step, world_state=world_state, user_prompt=user_prompt,
                              user_note=user_note, config=config, services=self)
            data = await custom(ctx)
            await self._hook_registry.dispatch_step(step_id, data, world_state, user_prompt)
            return data

        uses = getattr(step, "uses", "llm")
        if step_id == "map_generation" or uses == USES_MAP:
            return self._map_gen.generate(world_state, config)

        if self._llm_service and self._llm_service.mode != "mock":
            context = self._build_chain_context(world_state, step_id)
            data = await self._llm_gen.generate(step, context, user_prompt, user_note)
        else:
            data = self._mock_gen.generate(step, world_state, user_prompt, user_note)

        await self._hook_registry.dispatch_step(step_id, data, world_state, user_prompt)
        return data

    async def regenerate_list_item(
        self, step_id: str, field: str, items: list, index: int,
        world_state: dict, user_prompt: str, user_note: str = "",
    ) -> str:
        """Regenerate a single entry of a step's string-list field.

        Stateless: returns the new entry string; the caller is responsible for
        splicing it into the field value and persisting it.
        """
        step = self._steps.get(step_id)
        if not step:
            raise ValueError(f"Unknown step: {step_id}")
        field_schema = (step.schema or {}).get(field)
        if not isinstance(field_schema, dict):
            raise ValueError(f"Unknown field '{field}' on step '{step_id}'")

        items = list(items or [])

        if self._llm_service and self._llm_service.mode != "mock":
            context = self._build_chain_context(world_state, step_id)
            return await self._llm_gen.generate_list_item(
                step, field, field_schema, items, index, context, user_prompt, user_note,
            )

        # Mock fallback: deterministic, distinct from existing entries.
        label = field_schema.get("label", field)
        existing = {str(it).strip().lower() for it in items}
        n = len(items)
        while True:
            candidate = f"A freshly conjured {label.lower()} (variant {n + 1})"
            if candidate.lower() not in existing:
                return candidate
            n += 1

    # --- compilation --------------------------------------------------------

    def _merge_geography_steps(self, steps_data: dict) -> dict:
        return compiler.merge_geography_steps(steps_data)

    def compile_world(self, world_state: dict) -> dict:
        return compiler.compile_world(world_state, self._steps)

    # --- persistence (delegated) -------------------------------------------

    def list_worlds(self) -> list[dict]:
        return self._persistence.list_worlds()

    def save_world(self, world_id: str, world_state: dict) -> str:
        return self._persistence.save_world(world_id, world_state)

    def save_draft(self, world_id: str, world_state: dict) -> str:
        return self._persistence.save_draft(world_id, world_state)

    def load_world(self, world_id: str) -> dict:
        return self._persistence.load_world(world_id)

    def save_step(self, world_id: str, step_id: str, step_data: dict):
        return self._persistence.save_step(world_id, step_id, step_data)

    def delete_world(self, world_id: str):
        return self._persistence.delete_world(world_id)

    def seed_world(self, seed_prompt: str, world_id: str = None, total_nodes: int = 60) -> dict:
        import uuid
        safe_id = world_id or uuid.uuid4().hex[:8]
        safe_id = "".join(c for c in safe_id.lower().replace(" ", "_") if c.isalnum() or c in "_-")

        world_state = {"seed_prompt": seed_prompt, "steps": {}, "complete": False, "current_step": None}
        from backend.engine.worldgen.fixtures.mock_data import MOCK_GENERATORS

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
            if step_id == "layer_design" and isinstance(data, dict) and data.get("layers"):
                note_for_layer = "multi-layer world"

        world_id = self.save_world(safe_id, world_state)
        compiled = self.compile_world(world_state)
        return {
            "world_id": world_id,
            "seed_prompt": seed_prompt,
            "step_count": len(world_state["steps"]),
            "compiled_keys": list(compiled.keys()),
            "total_map_nodes": sum(
                len(ml.get("map", {}).get("nodes", [])) for ml in compiled.get("map_layers", [])
            ) if compiled.get("map_layers") else len(compiled.get("map", {}).get("nodes", [])),
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

    async def enrich_next_label(self, world_id: str, labeled_node_ids: list = None, layer_filter: str = None) -> dict:
        return await self._enrichment.label_next(world_id, labeled_node_ids, layer_filter)

    async def enrich_next_description(self, world_id: str, labeled_node_ids: list = None, layer_filter: str = None) -> dict:
        return await self._enrichment.describe_next(world_id, labeled_node_ids, layer_filter)

    # --- start locations ----------------------------------------------------

    def get_start_locations(self, world_id: str) -> list[dict]:
        compiled = self.compile_world(self.load_world(world_id))
        return _start.get_start_locations(compiled)

    async def llm_pick_start_location(self, world_id: str, preference: str, llm):
        compiled = self.compile_world(self.load_world(world_id))
        candidates = _start.get_start_locations(compiled)
        return await _start.llm_pick_start_location(compiled, candidates, preference, llm)

    # --- mock fixtures (kept as methods for direct callers/tests) ----------

    def _mock_rules(self, prompt: str, note: str = "") -> dict:
        return mock_rules(prompt, note)

    def _mock_lore(self, prompt: str, note: str = "") -> dict:
        return mock_lore(prompt, note)

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
