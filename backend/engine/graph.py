from langgraph.graph import StateGraph, END
from backend.engine.state import WorldState
from backend.sdk.mock_sdk import WorldBoxSDK, ValidationVeto
from backend.engine.registry import ModuleRegistry
from backend.engine.llm import LLMService
from backend.engine.memory import MemoryManager
from backend.engine.prompt_pipeline import PromptCompiler, build_auto_player_action_prompt
from backend.engine.settings_registry import SettingsRegistry
from backend.engine.provider_manager import ProviderManager
from copy import deepcopy
import asyncio
import json
import os

VETO_MAX_RETRIES = 3

# Player-identity fields modules may write back via the sanctioned
# ``character_update`` result key (collected by the librarian node and the
# slash-command dispatcher). Stats, skills and HP remain owned by module_data.
CHARACTER_UPDATE_FIELDS = (
    "name", "gender", "race", "short_appearance", "full_appearance", "personality",
)

DEFAULT_STAT_TIERS = [
    {"min": 1, "max": 4, "label": "Severely Impaired"},
    {"min": 5, "max": 8, "label": "Below Average"},
    {"min": 9, "max": 12, "label": "Average"},
    {"min": 13, "max": 16, "label": "Above Average / Trained"},
    {"min": 17, "max": 20, "label": "Expert / Peak Human"},
    {"min": 21, "max": 25, "label": "Superhuman"},
    {"min": 26, "max": 30, "label": "Legendary / Demigod"},
]

def _stat_tier_label(val: int, tiers: list[dict]) -> str:
    for t in tiers:
        if val >= t["min"] and val <= t["max"]:
            return t["label"]
    return "Unknown"

class EngineGraph:
    def __init__(self, registry: ModuleRegistry, settings_registry: SettingsRegistry = None, provider_manager: ProviderManager = None):
        self.registry = registry
        self.sdk = WorldBoxSDK()
        self.settings = settings_registry or SettingsRegistry()
        self.provider_manager = provider_manager or ProviderManager()
        self.llm = LLMService()
        print(f"[DEBUG] EngineGraph.__init__: LLMService created, reader_model='{self.llm.reader_model}', storyteller_model='{self.llm.storyteller_model}'")
        self.provider_manager.set_llm_service(self.llm)
        print(f"[DEBUG] EngineGraph.__init__: after set_llm_service, reader_model='{self.llm.reader_model}', storyteller_model='{self.llm.storyteller_model}'")
        self.prompt_compiler = PromptCompiler()
        self.memory = None # Lazy initialized
        # Bound to the active save before any turn runs; None while no story
        # is loaded.
        self.memory_db_path = None
        # Story-source providers registered by modules (e.g. wb_worldgen). Maps a
        # source type -> async provider used by create_save to build a story.
        self.story_sources = {}
        self.sdk.llm._set_service(self.llm)
        self.sdk.memory._set_engine(self)
        self._register_settings()
        
        workflow = StateGraph(WorldState)
        workflow.add_node("gather_context", self.gather_context_node)
        workflow.add_node("storyteller", self.storyteller_node)
        workflow.add_node("reader", self.reader_node)
        workflow.add_node("librarian", self.librarian_node)
        
        workflow.set_entry_point("gather_context")
        workflow.add_edge("gather_context", "storyteller")
        workflow.add_edge("storyteller", "reader")
        workflow.add_conditional_edges(
            "reader",
            self._check_veto,
            {
                "rewrite": "storyteller",
                "librarian": "librarian",
            }
        )
        workflow.add_edge("librarian", END)
        
        self.app = workflow.compile()

    def _register_settings(self):
        self.settings.register(
            "librarian.frequency", "slider", 3,
            label="Memory Generation Frequency",
            category="Memory",
            description="Generate new structured AI memories every N turns",
            min=1, max=20,
        )
        self.settings.register(
            "librarian.chunk_size", "slider", 3,
            label="Memory Chunk Size",
            category="Memory",
            description="Number of recent history turns to summarize per memory chunk",
            min=1, max=10,
        )
        self.settings.register(
            "memory.rag_limit", "slider", 3,
            label="RAG Memory Limit",
            category="Memory",
            description="Max number of past memories to inject into each turn's context",
            min=1, max=10,
        )
        self.settings.register(
            "world.narrative_style", "text", "exploration-driven",
            label="Narrative Style",
            category="World Building",
            description="The narrative style applied to all generated worlds (e.g. exploration-driven, character-focused, plot-driven)",
            is_global=True,
        )
        self.settings.register(
            "world.rag_limit", "slider", 2,
            label="World RAG Limit",
            category="World Building",
            description="Max number of world knowledge entries to retrieve per turn",
            min=0, max=10,
        )
        self.settings.register(
            "storyteller.auto_mode", "toggle", False,
            label="Storyteller Auto Mode",
            category="Storyteller",
            description="Let the AI play your character: a fast model decides their next action from their personality and the story, and anything you type becomes hidden guidance for that decision instead of an in-character action.",
        )

    def register_story_source(self, source_type: str, provider):
        """Register a story-source provider (e.g. the world module's world source).

        ``provider`` is an async callable invoked by create_save to turn a
        selected source id into a playable save (producing world_data, location,
        RAG index, etc.).
        """
        self.story_sources[source_type] = provider

    def set_memory_path(self, memory_db_path: str):
        if memory_db_path != self.memory_db_path:
            self.memory_db_path = memory_db_path
            self.memory = None

    def close_memory(self):
        if self.memory is not None:
            self.memory.close()
            self.memory = None
    
    def set_world_index_path(self, world_index_path: str):
        pass

    def rollback_memory(self, target_turn: int) -> bool:
        if self.memory is None:
            return False
        self.memory.rollback_memories(target_turn)
        return True

    def _deep_merge(self, base: dict, update: dict) -> dict:
        merged = dict(base)
        for key, value in update.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _get_module_execution_levels(self) -> list[list[str]]:
        modules = self.registry.get_modules()
        if not modules:
            return []

        deps = {}
        for mod_id, mod_data in modules.items():
            deps[mod_id] = set(mod_data["manifest"].get("dependencies", []))

        levels = []
        remaining = set(deps.keys())
        satisfied = set()

        while remaining:
            ready = {mid for mid in remaining if deps[mid].issubset(satisfied)}
            if not ready:
                print(f"[Engine] Unresolved module dependencies (cycles or missing): {remaining}. Running sequentially as fallback.")
                levels.append(sorted(remaining))
                break
            levels.append(sorted(ready))
            remaining -= ready
            satisfied |= ready

        return levels

    async def _run_modules_in_levels(
        self,
        hook_name: str,
        state: dict,
        build_args=None,
        collect=None,
        merge_module_data: bool = True,
    ):
        levels = self._get_module_execution_levels()
        accumulated_state = deepcopy(state)

        # A save may restrict which modules are active (chosen at story start via
        # the module toggle). When the reserved key is present, modules outside
        # the active set are skipped entirely for this save.
        active_modules = accumulated_state.get("module_configs", {}).get("__active_modules__")
        active_set = set(active_modules) if isinstance(active_modules, list) else None

        for level in levels:
            tasks = {}
            task_meta = {}

            for mod_id in level:
                if mod_id not in self.registry.get_modules():
                    continue
                if active_set is not None and mod_id not in active_set:
                    continue
                mod_data = self.registry.get_modules()[mod_id]
                backend = mod_data["backend"]
                hook_fn = getattr(backend, hook_name, None)
                if hook_fn is None:
                    continue

                manifest = mod_data["manifest"]
                consumes = manifest.get("consumes", {})
                module_state = self._build_module_state(accumulated_state, mod_id, consumes)

                extra = ()
                if build_args:
                    extra = build_args(mod_id, mod_data, module_state)

                tasks[mod_id] = self._safe_call_hook(mod_id, hook_fn, hook_name, module_state, *extra)
                task_meta[mod_id] = mod_data

            if not tasks:
                continue

            gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)

            for (mod_id, task), result in zip(tasks.items(), gathered):
                if isinstance(result, Exception):
                    print(f"Error in {mod_id}.{hook_name}: {result}")
                    continue
                if not result or not isinstance(result, dict):
                    continue

                mod_data = task_meta[mod_id]
                produces = mod_data["manifest"].get("produces", {})

                if merge_module_data and produces.get("module_data") and "module_data" in result:
                    accumulated_state.setdefault("module_data", {})
                    for key, val in result["module_data"].items():
                        accumulated_state["module_data"][key] = self._deep_merge(
                            accumulated_state["module_data"].get(key, {}),
                            val if isinstance(val, dict) else {},
                        )

                if collect:
                    collect(mod_id, mod_data, result, produces)

        return accumulated_state

    def _build_module_state(self, full_state: dict, mod_id: str, consumes: dict) -> dict:
        filtered = {}

        for key in ("active_save_id", "turn"):
            if key in full_state:
                filtered[key] = full_state[key]

        requested = consumes.get("state", [])
        if requested == "*":
            for key in full_state:
                if key not in ("module_data", "module_configs", "world_data"):
                    filtered[key] = full_state[key]
        else:
            for key in requested:
                if key in full_state:
                    filtered[key] = full_state[key]

        own_data = full_state.get("module_data", {}).get(mod_id)
        if own_data is not None:
            filtered.setdefault("module_data", {})[mod_id] = deepcopy(own_data)

        own_config = full_state.get("module_configs", {}).get(mod_id)
        if own_config is not None:
            filtered.setdefault("module_configs", {})[mod_id] = deepcopy(own_config)

        dep_data = consumes.get("module_data", [])
        if dep_data == "*":
            for did, dval in full_state.get("module_data", {}).items():
                if did != mod_id:
                    filtered.setdefault("module_data", {})[did] = deepcopy(dval)
        else:
            for did in dep_data:
                dval = full_state.get("module_data", {}).get(did)
                if dval is not None:
                    filtered.setdefault("module_data", {})[did] = deepcopy(dval)

        dep_cfg = consumes.get("module_configs", [])
        if dep_cfg == "*":
            for did, cval in full_state.get("module_configs", {}).items():
                if did != mod_id:
                    filtered.setdefault("module_configs", {})[did] = deepcopy(cval)
        else:
            for did in dep_cfg:
                cval = full_state.get("module_configs", {}).get(did)
                if cval is not None:
                    filtered.setdefault("module_configs", {})[did] = deepcopy(cval)

        if consumes.get("world_data"):
            wd = full_state.get("world_data")
            if wd is not None:
                filtered["world_data"] = wd

        return filtered

    async def _safe_call_hook(self, mod_id, hook_fn, hook_name, state, *extra_args):
        try:
            self.sdk.llm._current_module = mod_id
            return await hook_fn(*extra_args, state, self.sdk)
        except Exception as e:
            print(f"Error in {mod_id}.{hook_name}: {e}")
            return None
        finally:
            self.sdk.llm._current_module = ""

    async def ensure_memory(self):
        if self.memory is None:
            if not self.memory_db_path:
                raise RuntimeError("No story is loaded; the memory store has no save to bind to.")
            dummy_vector = await self.llm.get_embedding("init",
                inspector_ctx={"call_type": "embedding", "step": "ensure_memory"})
            dim_size = len(dummy_vector)
            print(f"[Engine] Initializing memory store with dynamic vector dimension: {dim_size}")
            self.memory = MemoryManager(self.memory_db_path, dim_size)

    async def initialize_module_data(self, state: dict) -> dict:
        accumulated = await self._run_modules_in_levels(
            "on_gather_context",
            state,
            merge_module_data=True,
        )
        state = dict(state)
        state["module_data"] = accumulated.get("module_data", dict(state.get("module_data", {})))
        return state

    async def _ensure_memory(self):
        await self.ensure_memory()

    async def generate_intro(self, state: dict, streaming_callback=None) -> dict:
        character = state.get("characters", {}).get("default_player", {})
        character_name = character.get("name", "Adventurer")
        module_data = state.get("module_data", {})
        world_data = state.get("world_data")
        scenario_data = state.get("scenario_data")

        # Basic scenario story source: when a scenario supplies a literal opening
        # message, the story opens with it verbatim (no LLM generation) — unless
        # the player requested modifications at story creation, in which case
        # the scenario is rewritten first and the opening is regenerated as a
        # streamed LLM response.
        if scenario_data:
            pending_request = (scenario_data.get("pending_modification_request") or "").strip()
            if pending_request:
                scenario_data = await self._rewrite_scenario_description(scenario_data, pending_request)
                state["scenario_data"] = scenario_data
                original_opening = (scenario_data.get("starting_prompt") or "").strip()
                if original_opening:
                    result = await self._rewrite_starting_prompt(
                        scenario_data, original_opening, pending_request, streaming_callback
                    )
                    # The streamed rewrite becomes both the opening message and
                    # the scenario's new literal starting prompt.
                    scenario_data["starting_prompt"] = result["content"]
                    return result
                # No literal opening: fall through to the generated intro, which
                # picks up the modified description below.
            else:
                starting_prompt = (scenario_data.get("starting_prompt") or "").strip()
                if starting_prompt:
                    # Emit the literal opening through the streaming callback so the
                    # client receives it the same way it receives LLM-generated text.
                    # Without this, no token is streamed and the frontend's `done`
                    # handler skips appending the message (it only appends streamed
                    # content), so the opening only appears after a reload.
                    if streaming_callback is not None:
                        await streaming_callback(starting_prompt)
                    # Return the same {content, reasoning} shape as the LLM path so
                    # the caller can read intro_result["content"] uniformly.
                    return {"content": starting_prompt, "reasoning": ""}

        parts = []

        parts.append("You are a creative storyteller crafting the opening scene of a text-based RPG adventure. Never mention stat names, numbers, or game mechanics — describe everything through immersive narrative prose.")

        character_lines = [f"<character>", f"Name: {character_name}"]
        if module_data:
            for mod_name, mod_state in module_data.items():
                if isinstance(mod_state, dict):
                    hp = mod_state.get("hp")
                    max_hp = mod_state.get("max_hp")
                    if hp is not None and max_hp is not None:
                        character_lines.append(f"HP: {hp}/{max_hp}")
                    stats = mod_state.get("stats")
                    if isinstance(stats, dict) and stats:
                        tier_list = state.get("module_configs", {}).get(mod_name, {}).get("stat_tiers", DEFAULT_STAT_TIERS) or DEFAULT_STAT_TIERS
                        stat_parts = []
                        for stat_name, stat_val in stats.items():
                            label = _stat_tier_label(stat_val, tier_list)
                            stat_parts.append(f"{stat_name} ({label})")
                        if stat_parts:
                            character_lines.append(f"Attributes: {', '.join(stat_parts)}")
                    level = mod_state.get("level")
                    if level is not None:
                        character_lines.append(f"Level: {level}")
        character_lines.append("</character>")
        parts.append("\n".join(character_lines))

        # World/setting context for the opening scene is contributed by modules
        # (e.g. wb_worldgen) via on_intro_context, respecting the active set.
        intro_active = state.get("module_configs", {}).get("__active_modules__")
        intro_active_set = set(intro_active) if isinstance(intro_active, list) else None
        for mod_id, mod_data in self.registry.get_modules().items():
            if intro_active_set is not None and mod_id not in intro_active_set:
                continue
            hook = getattr(mod_data["backend"], "on_intro_context", None)
            if hook is None:
                continue
            try:
                module_state = self._build_module_state(state, mod_id, mod_data["manifest"].get("consumes", {}))
                res = await hook(module_state, self.sdk)
                text = res if isinstance(res, str) else (res.get("content", "") if isinstance(res, dict) else "")
                if text:
                    parts.append(text)
            except Exception as e:
                print(f"Error in {mod_id} on_intro_context: {e}")

        if scenario_data:
            description = (scenario_data.get("scenario_description") or "").strip()
            if description:
                parts.append("<scenario>")
                parts.append(description)
                parts.append("</scenario>")

        parts.append("<instructions>")
        parts.append("Write the opening scene for this adventure. Do the following:")
        parts.append("1. Vividly introduce the world and its atmosphere — weave worldbuilding naturally into the prose.")
        parts.append("2. Describe the character's current situation, appearance, and immediate surroundings.")
        parts.append("3. Put events into motion — create an inciting incident, a mystery, or a compelling hook that draws the character toward action.")
        parts.append("4. Write 3-5 paragraphs of narrative prose. Do not use bullet points, lists, or XML tags in your output.")
        parts.append("5. End with an open-ended moment that invites the player to act, but do NOT ask a direct question like \"What do you do?\"")
        parts.append("</instructions>")

        system_content = "\n\n".join(parts)

        start_preference = state.get("start_preference", "")
        user_prompt = f'Begin the story. {start_preference}' if start_preference.strip() else "Begin the story."

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]

        return await self.llm.generate_story_from_messages(messages, streaming_callback,
            inspector_ctx={"call_type": "storyteller", "step": "generate_intro"},
            reasoning_callback=self.sdk.ui.emit_reasoning_token)

    async def _rewrite_scenario_description(self, scenario_data: dict, request_text: str) -> dict:
        """Apply a player's modification request to the save's scenario copy.

        Returns a new scenario dict with the pending request consumed and the
        description rewritten. Best-effort: on LLM/parse failure the original
        description is kept so the intro still proceeds.
        """
        await self.sdk.ui.emit_status("scenario_modify", "Adapting the scenario…")
        updated = {k: v for k, v in scenario_data.items() if k != "pending_modification_request"}
        updated["modified_by_request"] = True
        updated["applied_modification_request"] = request_text
        description = (scenario_data.get("scenario_description") or "").strip()
        if not description:
            return updated
        messages = [
            {"role": "system", "content": (
                "You revise role-play scenario descriptions. Apply the player's requested "
                "changes while keeping the same base premise, setting, and roughly the same "
                "length. Output only valid JSON: {\"scenario_description\": \"...\"}"
            )},
            {"role": "user", "content": (
                f"<scenario_description>\n{description}\n</scenario_description>\n\n"
                f"<requested_changes>\n{request_text}\n</requested_changes>"
            )},
        ]
        try:
            content = await self.llm.simple_completion(
                messages,
                response_format={"type": "json_object"},
                inspector_ctx={"call_type": "scenario_modify", "step": "rewrite_description"},
            )
            rewritten = (json.loads(content).get("scenario_description") or "").strip()
            if rewritten:
                updated["scenario_description"] = rewritten
        except Exception as e:
            print(f"[Engine] Scenario description rewrite failed, keeping original: {e}")
        return updated

    async def _rewrite_starting_prompt(self, scenario_data: dict, original_opening: str,
                                       request_text: str, streaming_callback=None) -> dict:
        parts = ["You are a creative storyteller adapting the opening message of a text-based RPG scenario."]
        description = (scenario_data.get("scenario_description") or "").strip()
        if description:
            parts.append("<scenario>")
            parts.append(description)
            parts.append("</scenario>")
        parts.append("<original_opening>")
        parts.append(original_opening)
        parts.append("</original_opening>")
        parts.append("<instructions>")
        parts.append("Rewrite the opening message so it satisfies the player's requested changes while keeping the original's base situation, voice, structure, and approximate length.")
        parts.append("Output only the rewritten opening as narrative prose — no preamble, lists, or XML tags.")
        parts.append("</instructions>")
        messages = [
            {"role": "system", "content": "\n\n".join(parts)},
            {"role": "user", "content": f"Requested changes: {request_text}"},
        ]
        return await self.llm.generate_story_from_messages(
            messages, streaming_callback,
            inspector_ctx={"call_type": "storyteller", "step": "rewrite_start_message"},
            reasoning_callback=self.sdk.ui.emit_reasoning_token,
        )

    def _check_veto(self, state: WorldState) -> str:
        if state.get("needs_rewrite") and state.get("veto_retries", 0) < VETO_MAX_RETRIES:
            return "rewrite"
        if state.get("needs_rewrite") and state.get("veto_retries", 0) >= VETO_MAX_RETRIES:
            print(f"[Engine] Veto retries exhausted ({VETO_MAX_RETRIES} attempts). Proceeding with fallback.")
            # Append a system note to history so user knows validation failed
            fallback_msg = f"[System: The Storyteller was unable to produce a response that passed all module validation rules after {VETO_MAX_RETRIES} attempts. The last generated response is shown above.]"
            state["history"] = state.get("history", []) + [fallback_msg]
            state["needs_rewrite"] = False
            state["veto_reason"] = None
        return "librarian"

    async def gather_context_node(self, state: WorldState):
        print("\n[Node: Gather Context] Collecting data from modules...")
        await self.sdk.ui.emit_status("gather_context", "Recalling memories…")
        gathered_context = []

        await self._ensure_memory()

        turn = state.get("turn", 0)
        retrieved_ids = []
        last_context_query = ""

        input_text = state.get("input_text", "")
        if input_text:
            last_context_query = input_text
            try:
                query_vector = await self.llm.get_embedding(input_text,
                    inspector_ctx={"call_type": "embedding", "step": "gather_context_node"})
                rag_limit = self.settings.get("memory.rag_limit")
                memories = self.memory.search_memories(query_vector, turn, limit=rag_limit)
                if memories:
                    memory_block = "<rag_memories>\n"
                    for m in memories:
                        memory_block += f"- {m['text']}\n"
                        retrieved_ids.append(m.get("id", ""))
                    memory_block += "</rag_memories>"
                    gathered_context.append(memory_block)

                world_rag_limit = self.settings.get("world.rag_limit")
                if world_rag_limit > 0 and self.memory.has_world_index():
                    # Build a location-enriched query so vague inputs (e.g. "I look around")
                    # still surface entries relevant to the player's current position.
                    location_hints = " ".join(filter(None, [
                        state.get("player_location_region", ""),
                        state.get("player_location_layer_id", ""),
                    ]))
                    if location_hints:
                        world_query_text = f"{input_text} {location_hints}".strip()
                        world_query_vector = await self.llm.get_embedding(
                            world_query_text,
                            inspector_ctx={"call_type": "embedding", "step": "gather_context_node_world"},
                        )
                    else:
                        world_query_vector = query_vector
                    world_entries = self.memory.search_world(world_query_vector, limit=world_rag_limit)
                    if world_entries:
                        world_block = "<world_knowledge>\n"
                        for we in world_entries:
                            world_block += f"- [{we['source_type']}] {we['text']}\n"
                        world_block += "</world_knowledge>"
                        gathered_context.append(world_block)
            except Exception as e:
                print(f"Error fetching RAG memories: {e}")

        # Location/world context is contributed by modules (e.g. wb_worldgen) via
        # on_gather_context -> context_string, collected below.
        context_strings = []

        def collect_gather(mod_id, mod_data, result, produces):
            nonlocal context_strings
            if produces.get("context_string") and result.get("context_string"):
                context_strings.append(f"<{mod_id}>\n{result['context_string']}\n</{mod_id}>")

        accumulated = await self._run_modules_in_levels(
            "on_gather_context",
            state,
            collect=collect_gather,
            merge_module_data=True,
        )

        gathered_context.extend(context_strings)

        return_val = {
            "current_context": gathered_context,
            "last_retrieved_memory_ids": retrieved_ids,
            "last_context_query": last_context_query,
            "module_data": accumulated.get("module_data", dict(state.get("module_data", {}))),
        }

        return return_val

    async def storyteller_node(self, state: WorldState):
        print("\n[Node: Storyteller] Assembling prompt and calling LLM...")

        module_prompt_blocks = await self._module_prompt_blocks(state)

        needs_rewrite = state.get("needs_rewrite", False)
        veto_retries = state.get("veto_retries", 0)

        await self.sdk.ui.emit_status(
            "storyteller",
            "Rewriting the response…" if needs_rewrite else "Writing the story…",
        )

        if needs_rewrite and veto_retries < VETO_MAX_RETRIES:
            veto_reason = state.get("veto_reason", "Unknown validation failure")
            print(f"[Node: Storyteller] REWRITE attempt {veto_retries + 1}/{VETO_MAX_RETRIES}: {veto_reason[:100]}")
            compiled_prompt = self.prompt_compiler.compile(
                state,
                module_blocks=module_prompt_blocks,
                validation_veto=f"PREVIOUS RESPONSE REJECTED by module validation. REASON: {veto_reason}\n\nRewrite your narration. The rejected action must not appear in the new response."
            )
        else:
            compiled_prompt = self.prompt_compiler.compile(state, module_blocks=module_prompt_blocks)

        # Don't stream on veto rewrites — the first attempt already sent tokens to
        # the client, so streaming again would produce a second visible response.
        story_result = await self.llm.generate_story_from_messages(
            compiled_prompt["messages"],
            streaming_callback=None if needs_rewrite else self.sdk.ui.emit_token,
            inspector_ctx={"call_type": "storyteller", "step": "storyteller_node"},
            reasoning_callback=None if needs_rewrite else self.sdk.ui.emit_reasoning_token,
        )
        story_output = story_result["content"]
        story_reasoning = story_result.get("reasoning", "")

        # The narration is done — finalize it on the client now so the message
        # renders immediately instead of waiting for the reader/librarian nodes.
        # Skip on veto rewrites (those don't stream and are replaced wholesale).
        if not needs_rewrite:
            await self.sdk.ui.emit_message_complete(story_output, story_reasoning)

        new_history = state.get("history", []) + [story_output]

        result = {"history": new_history, "last_prompt_trace": compiled_prompt["trace"], "needs_rewrite": False, "veto_reason": None, "last_reasoning": story_reasoning,
                  "last_model": story_result.get("model", ""), "last_usage": story_result.get("usage", {})}

        if needs_rewrite:
            result["veto_retries"] = veto_retries + 1
            result["needs_rewrite"] = False
            result["veto_reason"] = None

        # Dispatch on_validate_output to all active modules in parallel
        validate_tasks = {}
        merged_state = self._deep_merge(state, result)
        validate_active = merged_state.get("module_configs", {}).get("__active_modules__")
        validate_active_set = set(validate_active) if isinstance(validate_active, list) else None
        for mod_id, mod_data in self.registry.get_modules().items():
            if validate_active_set is not None and mod_id not in validate_active_set:
                continue
            backend = mod_data["backend"]
            if hasattr(backend, "on_validate_output"):
                module_state = self._build_module_state(merged_state, mod_id, mod_data["manifest"].get("consumes", {}))
                task = self._safe_validate(mod_id, backend.on_validate_output, story_output, module_state)
                validate_tasks[mod_id] = task

        if validate_tasks:
            gathered = await asyncio.gather(*validate_tasks.values(), return_exceptions=True)
            vetoes = []
            for (mod_id, task), ex in zip(validate_tasks.items(), gathered):
                if isinstance(ex, ValidationVeto):
                    print(f"[Node: Storyteller] VETO from {mod_id}: {ex.reason[:100]}")
                    vetoes.append(f"[{mod_id}] {ex.reason}")
                elif isinstance(ex, BaseException):
                    print(f"Error in {mod_id} on_validate_output: {ex}")

            if vetoes:
                result["needs_rewrite"] = True
                result["veto_reason"] = " | ".join(vetoes)
                if not needs_rewrite:
                    result["veto_retries"] = 0

        return result

    async def _safe_validate(self, mod_id, hook_fn, story_output, state):
        try:
            self.sdk.llm._current_module = mod_id
            await hook_fn(story_output, state, self.sdk)
        finally:
            self.sdk.llm._current_module = ""

    async def generate_auto_player_action(self, state: WorldState, nudge: str = "") -> str:
        """Storyteller auto mode: have the fast model play the player — decide
        the character's next action from their personality and the recent story
        (steered by the hidden ``nudge`` if the player typed one) and phrase it
        like a normal typed player message. Returns "" on failure so callers
        can fall back to a normal turn."""
        await self.sdk.ui.emit_status("auto_player", "Deciding your character's move…")
        prompt = build_auto_player_action_prompt(state, nudge)
        try:
            action = await self.sdk.llm.generate(prompt, model_preference="fastest")
        except Exception as exc:
            print(f"[Auto Mode] Player action generation failed: {exc}")
            return ""
        action = (action or "").strip().strip('"').strip()
        # A single short paragraph is expected; collapse any stray newlines.
        return " ".join(action.split())

    async def compile_prompt_preview(self, state: WorldState, prompt_pipeline: list[dict]) -> dict:
        module_prompt_blocks = await self._module_prompt_blocks(state)
        return self.prompt_compiler.compile(
            state,
            pipeline=prompt_pipeline,
            module_blocks=module_prompt_blocks,
        )

    async def _module_prompt_blocks(self, state: WorldState) -> list[dict]:
        blocks = []

        blocks_active = state.get("module_configs", {}).get("__active_modules__")
        blocks_active_set = set(blocks_active) if isinstance(blocks_active, list) else None
        for mod_id, mod_data in self.registry.get_modules().items():
            if blocks_active_set is not None and mod_id not in blocks_active_set:
                continue
            for manifest_block in mod_data["manifest"].get("prompt_blocks", []):
                block = deepcopy(manifest_block)
                local_block_id = block["id"]
                block["id"] = f"{mod_id}:{local_block_id}"
                block["source"] = f"module:{mod_id}"
                blocks.append((mod_id, mod_data, manifest_block, block))

        if not blocks:
            return []

        block_tasks = []
        block_indices = []

        for mod_id, mod_data, manifest_block, block in blocks:
            if block.get("type") != "module_prompt":
                block_tasks.append(None)
                block_indices.append((None, block))
                continue

            backend = mod_data["backend"]
            if not hasattr(backend, "on_render_prompt_block"):
                block.setdefault("config", {})["text"] = ""
                block_tasks.append(None)
                block_indices.append((None, block))
                continue

            manifest = mod_data["manifest"]
            consumes = manifest.get("consumes", {})
            module_state = self._build_module_state(state, mod_id, consumes)

            async def render_block(fn, mb, ms, mid):
                try:
                    self.sdk.llm._current_module = mid
                    result = await fn(mb, ms, self.sdk)
                    if isinstance(result, str):
                        return mid, result
                    elif isinstance(result, dict):
                        return mid, result.get("content") or result.get("text") or ""
                    return mid, ""
                except Exception as e:
                    print(f"Error in {mid} on_render_prompt_block: {e}")
                    return mid, ""
                finally:
                    self.sdk.llm._current_module = ""

            block_tasks.append(render_block(backend.on_render_prompt_block, manifest_block, module_state, mod_id))
            block_indices.append((mod_id, block))

        gathered = await asyncio.gather(
            *[t for t in block_tasks if t is not None], return_exceptions=True
        )

        task_idx = 0
        final_blocks = []
        for (mod_id, block), task in zip(block_indices, block_tasks):
            if task is None:
                final_blocks.append(block)
                continue
            result = gathered[task_idx]
            task_idx += 1
            if isinstance(result, Exception):
                print(f"Error in {mod_id} on_render_prompt_block: {result}")
                block.setdefault("config", {})["text"] = ""
            else:
                _, text = result
                block.setdefault("config", {})["text"] = text
            final_blocks.append(block)

        return final_blocks

    async def reader_node(self, state: WorldState):
        print("\n[Node: Reader] Parsing story output for state mutations...")
        await self.sdk.ui.emit_status("reader", "Updating the world…")

        latest_story = state["history"][-1]
        
        reader_active = state.get("module_configs", {}).get("__active_modules__")
        reader_active_set = set(reader_active) if isinstance(reader_active, list) else None

        schema = {}
        for mod_id, mod_data in self.registry.get_modules().items():
            if reader_active_set is not None and mod_id not in reader_active_set:
                continue
            mutation_schema = mod_data["manifest"].get("mutation_schema", {})
            if mutation_schema:
                schema[mod_id] = mutation_schema

        # Modules may offer a dynamic mutation schema (e.g. wb_worldgen movement,
        # whose options depend on the loaded world). Respect the active set.
        for mod_id, mod_data in self.registry.get_modules().items():
            if reader_active_set is not None and mod_id not in reader_active_set:
                continue
            dyn_hook = getattr(mod_data["backend"], "on_mutation_schema", None)
            if dyn_hook is None:
                continue
            try:
                module_state = self._build_module_state(state, mod_id, mod_data["manifest"].get("consumes", {}))
                dyn_schema = await dyn_hook(module_state, self.sdk)
                if isinstance(dyn_schema, dict) and dyn_schema:
                    schema[mod_id] = {**schema.get(mod_id, {}), **dyn_schema}
            except Exception as e:
                print(f"Error in {mod_id} on_mutation_schema: {e}")

        mutations = await self.llm.extract_mutations(latest_story, schema,
            inspector_ctx={"call_type": "reader", "step": "reader_node"}) if schema else {}
        print(f"[Node: Reader] Extracted mutations: {mutations}")
        
        state_update = {"module_data": dict(state.get("module_data", {}))}

        def build_mutate_args(mod_id, mod_data, module_state):
            return (mutations.get(mod_id, {}),)

        # Capture sanctioned location state keys a module's on_mutate_state may
        # return (e.g. wb_worldgen movement + fog-of-war reveal).
        location_update = {}

        def collect_location(mod_id, mod_data, result, produces):
            for key in ("player_location_node_id", "player_location_region",
                        "player_location_layer_id", "revealed_node_ids"):
                if key in result:
                    location_update[key] = result[key]

        accumulated = await self._run_modules_in_levels(
            "on_mutate_state",
            state,
            build_args=build_mutate_args,
            collect=collect_location,
            merge_module_data=True,
        )

        state_update["module_data"] = accumulated.get("module_data", state_update["module_data"])

        if location_update.get("player_location_node_id"):
            state_update.update(location_update)
            print(f"[Node: Reader] Player moved to node={location_update.get('player_location_node_id')}, "
                  f"region={location_update.get('player_location_region')}, "
                  f"layer={location_update.get('player_location_layer_id')}")

        turn = state.get("turn", 0) + 1
        
        # Preserve the turn's input for post-turn phases (librarian modules like
        # the character tracker need to see what the player actually declared).
        return self._deep_merge(state_update, {"current_context": [], "input_text": "",
                                               "last_input_text": state.get("input_text", ""), "turn": turn})

    async def librarian_node(self, state: WorldState):
        await self.sdk.ui.emit_status("librarian", "Recording memories…")
        await self._ensure_memory()

        turn = state.get("turn", 1)
        history = state.get("history", [])
        result = {}
        
        # Run memory pruning
        self.memory.purge_decayed_memories(turn)
        
        # Run semantic extraction based on configured frequency
        frequency = self.settings.get("librarian.frequency")
        if turn % frequency == 0 and history:
            print(f"\n[Node: Librarian] Summarizing recent history at turn {turn} (frequency={frequency})...")
            chunk_size = min(self.settings.get("librarian.chunk_size"), len(history))
            recent_texts = history[-chunk_size:]
            combined_text = "\n".join(recent_texts)
            turn_start = max(1, turn - chunk_size + 1)
            turn_range = f"turns {turn_start}-{turn}"
            
            try:
                memory_summary = await self.llm.summarize_memory_structured(combined_text, turn_range,
                    inspector_ctx={"call_type": "librarian", "step": "librarian_node:summary"})
                vector = await self.llm.get_embedding(memory_summary.summary,
                    inspector_ctx={"call_type": "embedding", "step": "librarian_node:embed"})
                importance = await self.llm.score_memory_importance_structured(
                    memory_summary.summary, memory_summary.entities, memory_summary.topics,
                    inspector_ctx={"call_type": "librarian", "step": "librarian_node:importance"},
                )
                
                memory_id = self.memory.add_memory(
                    vector=vector,
                    text=combined_text,
                    turn=turn,
                    importance=importance.importance,
                    summary=memory_summary.summary,
                    entities=memory_summary.entities,
                    topics=memory_summary.topics,
                    turn_range=memory_summary.turn_range or turn_range,
                    reason=importance.reason,
                    permanent=importance.permanent,
                )
                print(f"[Node: Librarian] Added memory (importance={importance.importance}, permanent={importance.permanent}): {memory_summary.summary[:120]}")
                result["last_stored_memory_id"] = memory_id
            except Exception as e:
                print(f"[Node: Librarian] Error generating memory: {e}")
        
        # Capture sanctioned canonical-character updates a module's on_librarian may
        # return (e.g. wb_character_tracker evolving appearance/identity/personality).
        # Only the whitelisted identity fields are accepted from modules.
        character_update = {}

        def collect_character(mod_id, mod_data, result_, produces):
            update = result_.get("character_update")
            if isinstance(update, dict):
                for key in CHARACTER_UPDATE_FIELDS:
                    val = update.get(key)
                    if val:
                        character_update[key] = val

        # Dispatch on_librarian to all modules in parallel for post-storyteller processing
        accumulated = await self._run_modules_in_levels(
            "on_librarian",
            state,
            collect=collect_character,
            merge_module_data=True,
        )

        if accumulated.get("module_data"):
            result["module_data"] = self._deep_merge(
                result.get("module_data", {}),
                accumulated["module_data"],
            )

        if character_update:
            characters = deepcopy(state.get("characters", {}))
            player = characters.get("default_player")
            if isinstance(player, dict):
                player.update(character_update)
                characters["default_player"] = player
                result["characters"] = characters
                print(f"[Node: Librarian] Player character updated: {', '.join(character_update.keys())}")

        return result
