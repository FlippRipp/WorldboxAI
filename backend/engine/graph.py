from langgraph.graph import StateGraph, END
from backend.engine.state import WorldState
from backend.sdk.mock_sdk import WorldBoxSDK, ValidationVeto
from backend.engine.registry import ModuleRegistry
from backend.engine.llm import LLMService
from backend.engine.memory import MemoryManager
from backend.engine.prompt_pipeline import PromptCompiler
from backend.engine.settings_registry import SettingsRegistry
from backend.engine.provider_manager import ProviderManager
from copy import deepcopy
import asyncio
import os

VETO_MAX_RETRIES = 3

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
        self.memory_db_path = "data/saves/autosave/vector_index"
        self.sdk.llm._set_service(self.llm)
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

    def set_memory_path(self, memory_db_path: str):
        if memory_db_path != self.memory_db_path:
            self.memory_db_path = memory_db_path
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

        for level in levels:
            tasks = {}
            task_meta = {}

            for mod_id in level:
                if mod_id not in self.registry.get_modules():
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
            dummy_vector = await self.llm.get_embedding("init",
                inspector_ctx={"call_type": "embedding", "step": "ensure_memory"})
            dim_size = len(dummy_vector)
            print(f"[Engine] Initializing LanceDB with dynamic vector dimension: {dim_size}")
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

    async def generate_intro(self, state: dict, streaming_callback=None) -> str:
        character = state.get("characters", {}).get("default_player", {})
        character_name = character.get("name", "Adventurer")
        module_data = state.get("module_data", {})
        world_data = state.get("world_data")

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

        if world_data:
            rules = world_data.get("rules", {})
            lore = world_data.get("lore", {})
            if rules:
                parts.append("<world_rules>")
                parts.append(f"Genre: {rules.get('genre', 'N/A')}")
                parts.append(f"Tone: {rules.get('tone', 'N/A')}")
                parts.append(f"Magic Level: {rules.get('magic_level', 'N/A')}")
                parts.append(f"Technology Era: {rules.get('tech_era', 'N/A')}")
                parts.append(f"Lethality: {rules.get('lethality', 'N/A')}/10")
                custom_rules = rules.get("custom_rules", [])
                if custom_rules:
                    parts.append("Custom Rules:")
                    for rule in custom_rules:
                        parts.append(f"  - {rule}")
                parts.append("</world_rules>")
            if lore:
                parts.append("<world_premise>")
                world_name = lore.get("world_name", "")
                if world_name:
                    parts.append(f"World: {world_name}")
                premise = lore.get("premise", "")
                if premise:
                    parts.append(premise)
                central_conflict = lore.get("central_conflict", "")
                if central_conflict:
                    parts.append(f"Central Conflict: {central_conflict}")
                creation_myth = lore.get("creation_myth", "")
                if creation_myth:
                    parts.append(f"Creation Myth: {creation_myth}")
                eras = lore.get("historical_eras", [])
                if eras:
                    parts.append("Historical Eras:")
                    for era in eras:
                        parts.append(f"  - {era.get('name', '')}: {era.get('summary', '')}")
                parts.append("</world_premise>")

            location_text = self._build_location_context(state, world_data)
            if location_text:
                parts.append(location_text)

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
            inspector_ctx={"call_type": "storyteller", "step": "generate_intro"})

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

        world_data = state.get("world_data")
        if world_data:
            location_context = self._build_location_context(state, world_data)
            if location_context:
                gathered_context.append(location_context)

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

    def _build_location_context(self, state: dict, world_data: dict) -> str:
        node_id = state.get("player_location_node_id")
        region_name = state.get("player_location_region")
        layer_id = state.get("player_location_layer_id")
        nodes = world_data.get("map", {}).get("nodes", [])
        map_layers = world_data.get("map_layers", [])
        regions = world_data.get("regions", {}).get("regions", [])
        layer_info = world_data.get("layers", [])

        if map_layers:
            all_nodes = []
            for layer in map_layers:
                all_nodes.extend(layer.get("map", {}).get("nodes", []))
            nodes = all_nodes

        current_node = None
        for n in nodes:
            if n.get("id") == node_id:
                current_node = n
                break

        current_region = None
        if region_name:
            for r in regions:
                if r.get("name") == region_name:
                    current_region = r
                    break

        current_layer = None
        if layer_id:
            for layer in layer_info:
                if layer.get("layer_id") == layer_id:
                    current_layer = layer
                    break

        if not current_node and not current_region and not current_layer:
            return ""

        parts = ["<current_location>"]
        if current_layer:
            parts.append(f"Layer: {current_layer.get('name', layer_id)} — {current_layer.get('description', '')[:300]}")
            # Inject layer-specific rules
            layer_rules = world_data.get("layer_rules", [])
            for lr in layer_rules:
                if lr.get("layer_id") == layer_id:
                    rules = lr.get("rules", [])
                    if rules:
                        parts.append("<layer_rules>")
                        for rule in rules:
                            parts.append(f"  - {rule}")
                        parts.append("</layer_rules>")
                    break
        if current_node:
            node_name = current_node.get("name", "")
            node_type = current_node.get("type", "location")
            node_desc = current_node.get("description", "")
            if node_name and node_desc:
                parts.append(f"Location: {node_name} ({node_type}) — {node_desc[:600]}")
            elif node_name:
                parts.append(f"Location: {node_name} ({node_type})")
            if current_node.get("interlayer_connection_id"):
                map_connections = world_data.get("map_connections", [])
                for lc in map_connections:
                    if lc.get("id") == current_node.get("interlayer_connection_id"):
                        target_layer = lc.get("to_layer_id") if lc.get("from_layer_id") == layer_id else lc.get("from_layer_id")
                        parts.append(f"Inter-layer connection: {lc.get('connection_type', 'passage')} to layer '{target_layer}' — {lc.get('description', '')[:200]}")
                        break
        if current_region:
            parts.append(f"Region: {current_region.get('name', '')}")
            parts.append(f"Terrain: {current_region.get('terrain', 'N/A')[:400]}")
            parts.append(f"Climate: {current_region.get('climate', 'N/A')[:200]}")
            landmarks = current_region.get("landmarks", [])
            if landmarks:
                parts.append(f"Nearby Landmarks: {', '.join(landmarks[:5])}")
            factions = current_region.get("factions", [])
            if factions:
                parts.append(f"Local Factions: {', '.join(factions[:5])}")
        if not current_region and region_name:
            parts.append(f"Region: {region_name}")
        parts.append("</current_location>")
        return "\n".join(parts)

    async def storyteller_node(self, state: WorldState):
        print("\n[Node: Storyteller] Assembling prompt and calling LLM...")

        module_prompt_blocks = await self._module_prompt_blocks(state)

        needs_rewrite = state.get("needs_rewrite", False)
        veto_retries = state.get("veto_retries", 0)

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

        story_output = await self.llm.generate_story_from_messages(
            compiled_prompt["messages"],
            streaming_callback=self.sdk.ui.emit_token,
            inspector_ctx={"call_type": "storyteller", "step": "storyteller_node"},
        )

        new_history = state.get("history", []) + [story_output]

        result = {"history": new_history, "last_prompt_trace": compiled_prompt["trace"], "needs_rewrite": False, "veto_reason": None}

        if needs_rewrite:
            result["veto_retries"] = veto_retries + 1
            result["needs_rewrite"] = False
            result["veto_reason"] = None

        # Dispatch on_validate_output to all modules in parallel
        validate_tasks = {}
        merged_state = self._deep_merge(state, result)
        for mod_id, mod_data in self.registry.get_modules().items():
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

    async def compile_prompt_preview(self, state: WorldState, prompt_pipeline: list[dict]) -> dict:
        module_prompt_blocks = await self._module_prompt_blocks(state)
        return self.prompt_compiler.compile(
            state,
            pipeline=prompt_pipeline,
            module_blocks=module_prompt_blocks,
        )

    async def _module_prompt_blocks(self, state: WorldState) -> list[dict]:
        blocks = []

        for mod_id, mod_data in self.registry.get_modules().items():
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
        
        latest_story = state["history"][-1]
        
        schema = {}
        for mod_id, mod_data in self.registry.get_modules().items():
            mutation_schema = mod_data["manifest"].get("mutation_schema", {})
            if mutation_schema:
                schema[mod_id] = mutation_schema

        if state.get("world_id"):
            world_data = state.get("world_data", {})
            schema["_world"] = self._build_location_mutation_schema(world_data)

        mutations = await self.llm.extract_mutations(latest_story, schema,
            inspector_ctx={"call_type": "reader", "step": "reader_node"}) if schema else {}
        print(f"[Node: Reader] Extracted mutations: {mutations}")
        
        state_update = {"module_data": dict(state.get("module_data", {}))}

        def build_mutate_args(mod_id, mod_data, module_state):
            return (mutations.get(mod_id, {}),)

        accumulated = await self._run_modules_in_levels(
            "on_mutate_state",
            state,
            build_args=build_mutate_args,
            merge_module_data=True,
        )

        state_update["module_data"] = accumulated.get("module_data", state_update["module_data"])
        
        world_mutations = mutations.get("_world", {})
        if world_mutations:
            new_node_id = world_mutations.get("player_location_node_id")
            new_region = world_mutations.get("player_location_region")
            new_layer_id = world_mutations.get("player_location_layer_id")
            if new_node_id and new_node_id != state.get("player_location_node_id"):
                state_update["player_location_node_id"] = new_node_id
                state_update["player_location_region"] = new_region or state.get("player_location_region")
                state_update["player_location_layer_id"] = new_layer_id or state.get("player_location_layer_id")
                print(f"[Node: Reader] Player moved to node={new_node_id}, region={new_region}, layer={new_layer_id}")

                revealed = list(set(state.get("revealed_node_ids", [])))
                world_data = state.get("world_data", {})
                adjacency = self._build_graph_adjacency(world_data)
                new_revealed = self._reveal_bfs(new_node_id, adjacency, radius=1)
                for nid in new_revealed:
                    if nid not in revealed:
                        revealed.append(nid)
                state_update["revealed_node_ids"] = revealed

        turn = state.get("turn", 0) + 1
        
        return self._deep_merge(state_update, {"current_context": [], "input_text": "", "turn": turn})

    def _build_location_mutation_schema(self, world_data: dict) -> dict:
        nodes = world_data.get("map", {}).get("nodes", [])
        map_layers = world_data.get("map_layers", [])
        if map_layers:
            all_nodes = []
            for layer in map_layers:
                all_nodes.extend(layer.get("map", {}).get("nodes", []))
            nodes = all_nodes
        regions = world_data.get("regions", {}).get("regions", [])
        location_options = []
        for n in nodes:
            if n.get("name"):
                location_options.append(f"{n['id']} ({n.get('name', '')})")
        if not location_options:
            location_options = ["any"]
        region_names = [r.get("name", "") for r in regions if r.get("name")]
        layers_list = world_data.get("layers", [])
        layer_options = [f"{l.get('layer_id', '')} ({l.get('name', '')})" for l in layers_list if l.get("layer_id")]
        if not layer_options:
            layer_options = ["surface"]
        return {
            "player_location_changed": {"type": "boolean", "label": "Did the player move to a new location?"},
            "player_location_node_id": {
                "type": "select",
                "label": "New location node ID",
                "options": location_options[:30],
                "description": "The node_id of the location the player moved to. Set only if player_location_changed is true."
            },
            "player_location_region": {
                "type": "select",
                "label": "New region name",
                "options": region_names[:20],
                "description": "The region the player moved into. Set only if player_location_changed is true."
            },
            "player_location_layer_id": {
                "type": "select",
                "label": "New layer ID",
                "options": layer_options[:10],
                "description": "The layer_id the player moved to (e.g., overworld, underground). Set only if the layer changed."
            },
        }

    def _build_graph_adjacency(self, world_data: dict) -> dict[str, list[str]]:
        edges = world_data.get("map", {}).get("edges", [])
        map_layers = world_data.get("map_layers", [])
        all_edges = list(edges)
        if map_layers:
            all_edges = []
            for layer in map_layers:
                all_edges.extend(layer.get("map", {}).get("edges", []))
        adj = {}
        for e in all_edges:
            fr, to = e.get("from"), e.get("to")
            if fr and to:
                adj.setdefault(fr, []).append(to)
                adj.setdefault(to, []).append(fr)
        return adj

    def _reveal_bfs(self, start_id: str, adjacency: dict[str, list[str]], radius: int) -> set[str]:
        visited = {start_id}
        frontier = [start_id]
        for _ in range(radius):
            next_frontier = []
            for nid in frontier:
                for nb in adjacency.get(nid, []):
                    if nb not in visited:
                        visited.add(nb)
                        next_frontier.append(nb)
            frontier = next_frontier
        return visited

    async def librarian_node(self, state: WorldState):
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
        
        # Dispatch on_librarian to all modules in parallel for post-storyteller processing
        accumulated = await self._run_modules_in_levels(
            "on_librarian",
            state,
            merge_module_data=True,
        )
        
        if accumulated.get("module_data"):
            result["module_data"] = self._deep_merge(
                result.get("module_data", {}),
                accumulated["module_data"],
            )
        
        return result
