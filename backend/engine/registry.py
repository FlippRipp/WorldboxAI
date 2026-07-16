import os
import json
import importlib.util
import logging
import re
from backend.engine.prompt_pipeline import ALLOWED_BLOCK_TYPES, ALLOWED_PLACEMENTS, ALLOWED_ROLES

logger = logging.getLogger(__name__)

ALLOWED_UI_SLOTS = {
    "slot_sidebar",
    "slot_header",
    "slot_chat_feed",
    "slot_modal",
    "slot_tab",
    "slot_message_footer",
}

ALLOWED_SETTING_TYPES = {"slider", "toggle", "select", "text"}

VALID_CONSUME_KEYS = {"state", "module_data", "module_configs", "world_data"}
VALID_PRODUCE_KEYS = {"module_data", "context_string", "messages"}
VALID_STATE_KEYS = {
    "input_text", "last_input_text", "turn", "history", "chat_messages", "characters",
    "world_id", "player_location_node_id", "player_location_region",
    "player_location_layer_id", "revealed_node_ids",
    "current_context", "prompt_pipeline", "last_prompt_trace",
    "needs_rewrite", "veto_retries", "veto_reason", "active_save_id",
    "story_style",
}


class ManifestValidationError(ValueError):
    pass

class ModuleRegistry:
    def __init__(self, modules_dir: str):
        self.modules_dir = modules_dir
        self.loaded_modules = {}

    def load_all_modules(self):
        self.loaded_modules = {}

        if not os.path.exists(self.modules_dir):
            logger.warning(f"Modules directory not found: {self.modules_dir}")
            return

        candidates = {}
        for item in sorted(os.listdir(self.modules_dir)):
            mod_path = os.path.join(self.modules_dir, item)
            if os.path.isdir(mod_path):
                manifest = self._read_manifest(mod_path, item)
                if not manifest:
                    continue

                manifest_id = manifest["id"]
                if manifest_id in candidates:
                    logger.error(f"Duplicate module id '{manifest_id}' in {item}; skipping duplicate.")
                    continue

                candidates[manifest_id] = {
                    "mod_name": item,
                    "path": mod_path,
                    "manifest": manifest,
                }

        for candidate in self._resolve_load_order(candidates):
            self._load_module_backend(candidate)

    def _read_manifest(self, mod_path: str, mod_name: str):
        manifest_path = os.path.join(mod_path, "manifest.json")
        backend_path = os.path.join(mod_path, "backend.py")
        
        if not os.path.exists(manifest_path) or not os.path.exists(backend_path):
            return None
             
        with open(manifest_path, 'r', encoding='utf-8') as f:
            try:
                manifest = json.load(f)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse manifest.json in {mod_name}")
                return None
        
        try:
            self._validate_manifest(manifest, mod_name)
        except ManifestValidationError as e:
            logger.error(f"Invalid manifest for {mod_name}: {e}")
            return None

        return manifest

    def _resolve_load_order(self, candidates: dict) -> list[dict]:
        ordered = []
        state = {}

        def visit(module_id: str, stack: list[str]) -> bool:
            current_state = state.get(module_id)
            if current_state == "loaded":
                return True
            if current_state == "skipped":
                return False

            state[module_id] = "visiting"
            dependencies = candidates[module_id]["manifest"].get("dependencies", [])
            for dependency_id in dependencies:
                if dependency_id not in candidates:
                    logger.error(f"Skipping {module_id}: missing dependency '{dependency_id}'.")
                    state[module_id] = "skipped"
                    return False

                if dependency_id in stack or state.get(dependency_id) == "visiting":
                    cycle_start = stack.index(dependency_id) if dependency_id in stack else 0
                    cycle = stack[cycle_start:] + [module_id]
                    logger.error(f"Skipping cyclic module dependencies: {' -> '.join(cycle)} -> {dependency_id}")
                    for skipped_id in set(cycle):
                        state[skipped_id] = "skipped"
                    return False

                if not visit(dependency_id, stack + [module_id]):
                    logger.error(f"Skipping {module_id}: dependency '{dependency_id}' could not be loaded.")
                    state[module_id] = "skipped"
                    return False

            state[module_id] = "loaded"
            ordered.append(candidates[module_id])
            return True

        for module_id in sorted(candidates):
            if state.get(module_id) is None:
                visit(module_id, [])

        return ordered

    def _load_module_backend(self, candidate: dict):
        mod_name = candidate["mod_name"]
        mod_path = candidate["path"]
        manifest = candidate["manifest"]
        backend_path = os.path.join(mod_path, "backend.py")

        # Dynamically load backend.py
        spec = importlib.util.spec_from_file_location(f"wb_module_{mod_name}", backend_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
                self.loaded_modules[manifest["id"]] = {
                    "manifest": manifest,
                    "backend": module,
                    "path": mod_path,
                    "router": self._extract_router(module, mod_name),
                }
                print(f"[Registry] Loaded module: {manifest.get('name', mod_name)}")
            except Exception as e:
                logger.error(f"Failed to execute module {mod_name}: {e}")

    def _extract_router(self, module, mod_name: str):
        """Return a FastAPI APIRouter exposed by a module's backend, if any.

        A module can own backend endpoints by exposing either a module-level
        ``router`` attribute or a ``get_router()`` factory. The router is mounted
        by the server under ``/api/modules/{mod_id}``.
        """
        router = None
        factory = getattr(module, "get_router", None)
        if callable(factory):
            try:
                router = factory()
            except Exception as e:
                logger.error(f"Module {mod_name} get_router() failed: {e}")
                return None
        else:
            router = getattr(module, "router", None)
        if router is None:
            return None
        # Duck-type check so registry stays import-light (no hard FastAPI dep).
        if not hasattr(router, "routes"):
            logger.error(f"Module {mod_name} exposed a non-router 'router'; ignoring.")
            return None
        return router

    def _validate_manifest(self, manifest: dict, mod_name: str):
        for field in ["id", "name", "version"]:
            if not isinstance(manifest.get(field), str) or not manifest[field].strip():
                raise ManifestValidationError(f"Missing or invalid required field '{field}'.")

        module_id = manifest["id"]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", module_id):
            raise ManifestValidationError("Module id must be lowercase snake_case and start with a letter.")

        self._validate_data_contract(manifest, mod_name)

        ui_slots = manifest.get("ui_slots", [])
        if not isinstance(ui_slots, list) or any(slot not in ALLOWED_UI_SLOTS for slot in ui_slots):
            raise ManifestValidationError(f"ui_slots must be a list containing only: {sorted(ALLOWED_UI_SLOTS)}")

        dependencies = manifest.get("dependencies", [])
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
            raise ManifestValidationError("dependencies must be a list of module id strings.")

        settings_schema = manifest.get("settings_schema", {})
        if not isinstance(settings_schema, dict):
            raise ManifestValidationError("settings_schema must be an object.")

        for setting_name, schema in settings_schema.items():
            if not isinstance(schema, dict):
                raise ManifestValidationError(f"settings_schema.{setting_name} must be an object.")
            setting_type = schema.get("type")
            if setting_type not in ALLOWED_SETTING_TYPES:
                raise ManifestValidationError(f"settings_schema.{setting_name}.type must be one of {sorted(ALLOWED_SETTING_TYPES)}.")
            if setting_type == "slider":
                for field in ["min", "max", "default"]:
                    if not isinstance(schema.get(field), (int, float)):
                        raise ManifestValidationError(f"settings_schema.{setting_name}.{field} must be numeric.")
            if setting_type == "toggle" and not isinstance(schema.get("default"), bool):
                raise ManifestValidationError(f"settings_schema.{setting_name}.default must be boolean.")

        mutation_schema = manifest.get("mutation_schema", {})
        if not isinstance(mutation_schema, dict):
            raise ManifestValidationError("mutation_schema must be an object.")

        prompt_blocks = manifest.get("prompt_blocks", [])
        if not isinstance(prompt_blocks, list):
            raise ManifestValidationError("prompt_blocks must be a list.")

        seen_prompt_block_ids = set()
        for index, block in enumerate(prompt_blocks):
            if not isinstance(block, dict):
                raise ManifestValidationError(f"prompt_blocks[{index}] must be an object.")

            block_id = block.get("id")
            if not isinstance(block_id, str) or not block_id.strip():
                raise ManifestValidationError(f"prompt_blocks[{index}].id must be a non-empty string.")
            if block_id in seen_prompt_block_ids:
                raise ManifestValidationError(f"Duplicate prompt block id in manifest: {block_id}")
            seen_prompt_block_ids.add(block_id)

            block_type = block.get("type")
            if block_type not in ALLOWED_BLOCK_TYPES:
                raise ManifestValidationError(f"prompt_blocks.{block_id}.type must be one of {sorted(ALLOWED_BLOCK_TYPES)}.")
            if block_type == "engine_context":
                raise ManifestValidationError("Module manifests cannot declare engine_context prompt blocks.")

            role_type = block.get("role_type")
            if role_type not in ALLOWED_ROLES:
                raise ManifestValidationError(f"prompt_blocks.{block_id}.role_type must be one of {sorted(ALLOWED_ROLES)}.")

            placement = block.get("placement")
            if placement not in ALLOWED_PLACEMENTS:
                raise ManifestValidationError(f"prompt_blocks.{block_id}.placement must be one of {sorted(ALLOWED_PLACEMENTS)}.")
            if placement == "chat_injection":
                depth = block.get("depth", 0)
                if not isinstance(depth, int) or depth < 0:
                    raise ManifestValidationError(f"prompt_blocks.{block_id}.depth must be a non-negative integer.")
                order = block.get("order")
                if order is not None and (isinstance(order, bool) or not isinstance(order, int)):
                    raise ManifestValidationError(f"prompt_blocks.{block_id}.order must be an integer.")

            config = block.get("config", {})
            if not isinstance(config, dict):
                raise ManifestValidationError(f"prompt_blocks.{block_id}.config must be an object.")
            if block_type == "static_text" and not isinstance(config.get("text"), str):
                raise ManifestValidationError(f"prompt_blocks.{block_id}.config.text must be a string.")

        modes = manifest.get("modes", [])
        if not isinstance(modes, list):
            raise ManifestValidationError("modes must be a list.")
        seen_mode_ids = set()
        for index, mode_entry in enumerate(modes):
            if not isinstance(mode_entry, dict):
                raise ManifestValidationError(f"modes[{index}] must be an object.")
            mode_id = mode_entry.get("id")
            if not isinstance(mode_id, str) or not mode_id.strip():
                raise ManifestValidationError(f"modes[{index}].id must be a non-empty string.")
            if mode_id in seen_mode_ids:
                raise ManifestValidationError(f"Duplicate mode id: {mode_id}")
            seen_mode_ids.add(mode_id)
            if not isinstance(mode_entry.get("label", ""), str):
                raise ManifestValidationError(f"modes[{index}].label must be a string.")
            screen = mode_entry.get("screen")
            if screen is not None and (not isinstance(screen, str) or not screen.endswith(".jsx")):
                raise ManifestValidationError(f"modes[{index}].screen must be a .jsx filename.")

        storyteller_start = manifest.get("storyteller_start")
        if storyteller_start is not None:
            if not isinstance(storyteller_start, dict):
                raise ManifestValidationError("storyteller_start must be an object.")
            st_screen = storyteller_start.get("screen")
            if not isinstance(st_screen, str) or not st_screen.endswith(".jsx"):
                raise ManifestValidationError("storyteller_start.screen must be a .jsx filename.")

        character_creation = manifest.get("character_creation")
        if character_creation is not None:
            if not isinstance(character_creation, dict):
                raise ManifestValidationError("character_creation must be an object.")
            default_state = character_creation.get("default_state")
            if default_state is not None and not isinstance(default_state, dict):
                raise ManifestValidationError("character_creation.default_state must be an object.")

    def _validate_data_contract(self, manifest: dict, mod_name: str):
        consumes = manifest.get("consumes")
        if not isinstance(consumes, dict):
            raise ManifestValidationError("consumes is required and must be an object.")

        for key in consumes:
            if key not in VALID_CONSUME_KEYS:
                raise ManifestValidationError(
                    f"Unknown consumes key '{key}'. Allowed: {sorted(VALID_CONSUME_KEYS)}"
                )

        state_req = consumes.get("state", [])
        if state_req != "*":
            if not isinstance(state_req, list) or any(
                k not in VALID_STATE_KEYS for k in state_req
            ):
                raise ManifestValidationError(
                    f"consumes.state must be a list of valid state keys or '*'. "
                    f"Valid keys: {sorted(VALID_STATE_KEYS)}"
                )

        for subkey in ("module_data", "module_configs"):
            val = consumes.get(subkey, [])
            if val != "*" and (not isinstance(val, list) or any(not isinstance(d, str) for d in val)):
                raise ManifestValidationError(
                    f"consumes.{subkey} must be a list of module id strings or '*'"
                )

        world_data = consumes.get("world_data")
        if not isinstance(world_data, bool):
            raise ManifestValidationError("consumes.world_data must be a boolean.")

        produces = manifest.get("produces")
        if not isinstance(produces, dict):
            raise ManifestValidationError("produces is required and must be an object.")

        for key in produces:
            if key not in VALID_PRODUCE_KEYS:
                raise ManifestValidationError(
                    f"Unknown produces key '{key}'. Allowed: {sorted(VALID_PRODUCE_KEYS)}"
                )
            if not isinstance(produces[key], bool):
                raise ManifestValidationError(f"produces.{key} must be a boolean.")

    def get_modules(self):
        return self.loaded_modules
