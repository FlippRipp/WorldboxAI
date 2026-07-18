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
from wbworldgen.worldgen.generation.llm import DEFAULT_SYSTEM_FRAMING
from wbworldgen.worldgen.enrichment import EnrichmentEngine, SiteExpansionEngine, collect_nodes_by_layer
from wbworldgen.worldgen.enrichment import maps_expand as _maps_expand
from wbworldgen.worldgen.enrichment import sites as _sites_mod
from wbworldgen.worldgen.generation import LLMStepGenerator, MapStepGenerator, MockStepGenerator
from wbworldgen.worldgen.hooks import HookRegistry
from wbworldgen.worldgen.persistence import WorldPersistence
from wbworldgen.worldgen.base import USES_MAP
from wbworldgen.worldgen.steps import hierarchy_design as _hierarchy_design
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


def scenario_start_brief(scenario: dict) -> str:
    """Render a scenario record as the start-location request used when a
    story combines a world with a scenario: the start location should be
    wherever the scenario's opening scene takes place.

    The player's pending modification request comes first and is marked
    highest-priority — it may move the opening somewhere the scenario text
    doesn't. Never truncated.
    """
    parts = []
    request = str(scenario.get("pending_modification_request") or "").strip()
    if request:
        parts.append(
            "The player's change request for this scenario — HIGHEST priority, "
            f"it overrides the scenario text below where they conflict:\n{request}")
    grounding = scenario_grounding_text(scenario)
    if grounding:
        parts.append(
            "The story starts with this scenario — choose the location where "
            f"its opening scene takes place (or the closest fit):\n{grounding}")
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


def _interview_history_text(history: list[dict] | None) -> str:
    """Render prior interview rounds (question/answer pairs) as plain text for
    the LLM. Skipped questions are shown as such — the player saw them and
    chose not to answer. Never truncated."""
    lines = []
    for pair in history or []:
        question = str(pair.get("question") or "").strip()
        if not question:
            continue
        answer = str(pair.get("answer") or "").strip()
        lines.append(f"Q: {question}\nA: {answer or '(skipped — the player left this open)'}")
    return "\n\n".join(lines)


def build_world_questions_messages(current_text: str = "",
                                   history: list[dict] | None = None,
                                   scenario: dict | None = None) -> list[dict]:
    """LLM messages for the world-prompt interview: ask the player a short
    round of clarifying questions about details the seed prompt leaves open.

    Works from an empty prompt too — the first round then asks foundational
    questions (genre, tone, scale, central conflict). Prior rounds are passed
    in `history` so the model never repeats itself, and a linked scenario is
    grounding so it never asks what the scenario already answers. Pure (no
    I/O) so it is unit-testable; the route feeds the result to the LLM.
    """
    system = (
        "You are a world-building assistant interviewing the player about the world "
        "they want an AI world generator to create. Read their seed prompt draft and "
        "ask 3-5 short, concrete questions about important details it leaves open — "
        "the things that would most change the generated world (tone, scale, conflict, "
        "magic or technology, factions, geography, cultures, history, what makes it "
        "distinct). Ask ONLY about the world itself — the setting the generator will "
        "build. Never ask about protagonists, individual characters, their goals or "
        "relationships, or how the story's plot unfolds: those belong to the scenario "
        "and the story, not to world generation. Each question must be answerable in "
        "a sentence or two. Never ask anything the prompt, the scenario, or a "
        "previous answer already settles, and never repeat a question from a previous "
        "round — a skipped question means the player wants to leave it open, so move "
        "on to something else. "
        'Return only valid JSON: {"questions": ["...", "..."]}.'
    )
    parts = []
    grounding = scenario_grounding_text(scenario) if scenario else ""
    if grounding:
        parts.append(
            "The world must fit this scenario the player has chosen — treat "
            "everything in it as already decided, not something to ask about. Its "
            "characters and events are story material, not open questions: ask "
            "about the wider world the scenario takes place in, never about the "
            "scenario's people or plot:\n"
            f"<scenario>\n{grounding}\n</scenario>")
    current_text = (current_text or "").strip()
    if current_text:
        parts.append(f"<current_world_prompt>\n{current_text}\n</current_world_prompt>")
    else:
        parts.append(
            "<current_world_prompt>\n(empty — the player hasn't written anything yet; "
            "ask foundational questions that help them shape the world from scratch)\n"
            "</current_world_prompt>")
    history_text = _interview_history_text(history)
    if history_text:
        parts.append(
            "Questions already asked in previous rounds — do not repeat or rephrase "
            f"any of these:\n<previous_rounds>\n{history_text}\n</previous_rounds>")
    parts.append("Ask the next round of questions. Return only the JSON.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def build_world_prompt_fold_messages(current_text: str,
                                     answers: list[dict],
                                     scenario: dict | None = None) -> list[dict]:
    """LLM messages for folding a round of interview answers into the seed
    prompt.

    Every answer must land in the prompt — added where it brings something
    new, rewriting whatever it changes — while parts the answers don't touch
    keep the player's wording. With an empty current prompt the answers become
    the first draft. Pure (no I/O) so it is unit-testable.
    """
    system = (
        "You are a world-building assistant maintaining the SEED PROMPT for an AI "
        "world generator — a short, vivid paragraph of creative direction the "
        "generator expands into a full world. The player has answered interview "
        "questions about their world; fold their answers into the prompt. Every "
        "answer must end up reflected in the prompt: add what it introduces, and "
        "rewrite whatever parts of the prompt it changes or contradicts — "
        "preserving the current text is never a reason to leave an answer out. "
        "Where the answers don't touch the prompt, keep the player's wording and "
        "details as they are, and do not pad or embellish beyond what the answers "
        "say. If the current prompt is empty, write a first draft from the answers "
        "alone. "
        'Return only valid JSON: {"text": "..."}.'
    )
    parts = []
    grounding = scenario_grounding_text(scenario) if scenario else ""
    if grounding:
        parts.append(
            "The world must fit this scenario the player has chosen — keep the "
            f"prompt consistent with it:\n<scenario>\n{grounding}\n</scenario>")
    current_text = (current_text or "").strip()
    if current_text:
        parts.append(f"<current_world_prompt>\n{current_text}\n</current_world_prompt>")
    else:
        parts.append("<current_world_prompt>\n(empty — write the first draft from the answers)\n</current_world_prompt>")
    answers_text = _interview_history_text(answers)
    parts.append(f"The player's answers this round:\n<answers>\n{answers_text}\n</answers>")
    parts.append(
        "Update the seed prompt so every answer is fully incorporated — add and "
        "change whatever the answers require, and keep the rest as the player "
        "wrote it. Return only the seed prompt text.")
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

    def _build_chain_context(self, world_state: dict, up_to_step_id: str) -> dict:
        return _pipeline.build_chain_context(self._ordered_ids, world_state, up_to_step_id, self._steps)

    def ordered_ids_for(self, world_state: dict) -> list[str]:
        """The effective step order for a world: the registered order minus
        the skips its own world_form design decided (abstract map style,
        AI-skipped optional steps). `resolve_order` itself stays untouched, so
        `after` chains keep resolving against the full registry."""
        skip = _world_form.dynamic_skips(world_state)
        return [sid for sid in self._ordered_ids if sid not in skip]

    def system_framing_for(self, world_state: dict) -> str:
        """The per-world system framing: the neutral default plus the world's
        own design (world_form's world_kind) as the genre voice. Worlds
        without a design (old worlds, the world_form step itself) keep the
        historical default framing byte-identical."""
        kind = _world_form.world_kind(world_state)
        if kind:
            return f"{DEFAULT_SYSTEM_FRAMING} This world: {kind}"
        return DEFAULT_SYSTEM_FRAMING

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

        uses = getattr(step, "uses", "llm")
        if step_id == "map_generation" or uses == USES_MAP:
            root_gen = self._root_generator_for(world_state)
            level = self._authored_root_level(world_state, root_gen)
            if level is not None:
                # Root-as-first-expansion: the whole world is one authored
                # place (an interior-style root level) — the same authored
                # flow expansion uses, minus a parent to anchor to.
                max_locations = self._resolve_enrichment_setting(
                    "world.site_max_sublocations", 10, 4, 16)
                return await self._maps_expand.expand_root(
                    world_state, user_prompt, level,
                    max_locations=max(8, max_locations))
            # Delaunay + road pathfinding are CPU-bound; keep the event loop free.
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, lambda: self._map_gen.generate(world_state, config, root_gen))

        if self._llm_service and self._llm_service.mode != "mock":
            context = self._build_chain_context(world_state, step_id)
            data = await self._llm_gen.generate(
                step, context, user_prompt, user_note,
                system_framing=self.system_framing_for(world_state),
                coverage_directive=_world_form.coverage_directive(world_state, step_id))
        else:
            data = self._mock_gen.generate(step, world_state, user_prompt, user_note)

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
        field_schema = (step.schema or {}).get(field)
        if not isinstance(field_schema, dict):
            raise ValueError(f"Unknown field '{field}' on step '{step_id}'")

        items = list(items or [])
        is_structured = isinstance(field_schema.get("item_schema"), dict)

        if self._llm_service and self._llm_service.mode != "mock":
            context = self._build_chain_context(world_state, step_id)
            framing = self.system_framing_for(world_state)
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

    def _authored_root_level(self, world_state: dict, root_gen: str) -> dict | None:
        """The designed root level when its generator needs authored (LLM)
        content — a world whose whole playable space is one interior-style
        map. None for procedural roots (terrain, abstract, city)."""
        from wbworldgen.worldgen.generation.registry import GENERATOR_REGISTRY
        spec = GENERATOR_REGISTRY.get(root_gen)
        if spec is None or not spec.needs_llm_content:
            return None
        designed = _hierarchy_design.designed_levels(world_state)
        if designed:
            return designed[0]
        return {"level_type": "interior", "label": "Interior", "generator_id": root_gen}

    def _root_generator_for(self, world_state: dict) -> str:
        """The generator that draws a world's root map. The world's own
        designed structure (hierarchy_design levels) is authoritative when
        present; worlds without one (old worlds, junk design output) fall
        back to the world_form "city" override over the default overworld
        generator, exactly as before the structure step existed."""
        designed = _hierarchy_design.designed_levels(world_state)
        if designed:
            return designed[0].get("generator_id") or "world_map"
        return _world_form.map_generator_override(world_state) or "world_map"

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
                root_gen = self._root_generator_for(world_state)
                level = self._authored_root_level(world_state, root_gen)
                if level is not None:
                    data = self._maps_expand.mock_root_map(world_state, level)
                else:
                    data = self._map_gen.generate(world_state, {"total_nodes": total_nodes},
                                                  root_gen)
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
                          force: bool = False, level_type: str = None) -> dict:
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
        compiled = self._enrichment._load_compiled(world_id)
        node = self._enrichment.get_node(world_id, node_id)
        if node is None:
            raise ValueError(f"Unknown map node: {node_id}")
        max_locations = self._resolve_enrichment_setting("world.site_max_sublocations", 10, 4, 16)
        child_nodes = self._resolve_enrichment_setting("world.child_map_nodes", 60, 20, 200)
        bundle = await self._maps_expand.expand(
            compiled, map_id, node, max_locations=max_locations,
            template_vocab=compiled.get("template_vocab"),
            level_type=level_type, total_nodes=child_nodes, world_id=world_id)
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

    async def author_location(self, world_id: str, description: str,
                              anchor_node_id: str = None) -> dict | None:
        """Author a brand-new named location matching a free-text description
        onto one of the world's unnamed map positions (one full-attention
        call) — used when the story needs a place that doesn't exist yet
        (e.g. a teleport to a named-but-unmapped destination).
        ``anchor_node_id`` (the player's current node) makes the placement
        spatially aware: slots are offered nearest-first so a place described
        relative to here lands nearby. Returns the candidate-shaped entry
        (node_id, map_id, ...) or None when no slot fits or the call fails."""
        if not self._llm_service or self._llm_service.mode == "mock":
            return None
        compiled = self._enrichment._load_compiled(world_id)
        try:
            authored = await _start.generate_start_location(
                compiled, description, description, self._llm_service,
                anchor_node_id=anchor_node_id)
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
