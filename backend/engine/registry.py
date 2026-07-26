import io
import os
import json
import importlib.util
import logging
import re
import shutil
import tempfile
import zipfile
from urllib.parse import urlparse
from backend.engine.prompt_pipeline import ALLOWED_BLOCK_TYPES, ALLOWED_PLACEMENTS, ALLOWED_ROLES

logger = logging.getLogger(__name__)

# Directories never considered when searching an extracted archive for a
# module root (VCS internals, build output, dependency trees).
_ARCHIVE_SCAN_IGNORE = {".git", ".github", "__pycache__", "node_modules", "venv", ".venv", "dist", "build"}

# Ceiling on the total uncompressed size of an installed module archive.
# Modules may ship data files, but a zip expanding past this is either a
# mistake or a decompression bomb.
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 500 * 1024 * 1024


class ModuleManagerError(ValueError):
    """A module-manager operation (enable/disable/install/remove) failed for a
    user-reportable reason. `.status` suggests an HTTP status for API callers."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def parse_github_url(url: str) -> dict:
    """Parse a GitHub repository URL into owner/repo/branch/subpath.

    Accepts the forms people actually paste:
      https://github.com/{owner}/{repo}
      https://github.com/{owner}/{repo}.git
      https://github.com/{owner}/{repo}/tree/{branch}
      https://github.com/{owner}/{repo}/tree/{branch}/{sub/path}

    Returns {"owner", "repo", "branch" (None for default), "subpath" (None)}.
    Raises ModuleManagerError for anything else.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in ("github.com", "www.github.com"):
        raise ModuleManagerError("Only github.com repository URLs are supported.")

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ModuleManagerError("GitHub URL must include owner and repository, e.g. https://github.com/user/repo.")

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        raise ModuleManagerError("GitHub URL must include owner and repository, e.g. https://github.com/user/repo.")

    branch = None
    subpath = None
    if len(parts) > 2:
        if parts[2] != "tree" or len(parts) < 4:
            raise ModuleManagerError("Unsupported GitHub URL. Use the repository root or a /tree/{branch}[/path] link.")
        branch = parts[3]
        if len(parts) > 4:
            subpath = "/".join(parts[4:])

    return {"owner": owner, "repo": repo, "branch": branch, "subpath": subpath}


def github_archive_url(owner: str, repo: str, branch: str | None) -> str:
    """The codeload zip URL for a repo. HEAD resolves the default branch."""
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    return f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"

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
    "player_location_map_id", "revealed_node_ids",
    "current_context", "prompt_pipeline", "last_prompt_trace",
    "needs_rewrite", "veto_retries", "veto_reason", "active_save_id",
    "story_style",
}


class ManifestValidationError(ValueError):
    pass

class ModuleRegistry:
    def __init__(self, modules_dir: str, state_path: str = None):
        self.modules_dir = modules_dir
        self.loaded_modules = {}
        # App-wide manager state. `state_path` (a JSON file) persists which
        # modules are disabled app-wide and which were installed through the
        # module manager. No file / no path means everything found is enabled.
        self.state_path = state_path
        self.discovered = {}   # mod_id -> {mod_name, path, manifest} for every valid module folder
        self.load_errors = {}  # mod_id -> reason an *enabled* discovered module is not loaded
        self._state = {"disabled": [], "installed": []}
        self._load_state()

    # ------------------------------------------------------------------
    # App-wide state persistence

    def _load_state(self):
        if not self.state_path or not os.path.isfile(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for key in ("disabled", "installed"):
                value = raw.get(key, [])
                if isinstance(value, list):
                    self._state[key] = [v for v in value if isinstance(v, str)]
        except Exception as e:
            logger.warning(f"Failed to load module state from {self.state_path}: {e}")

    def _save_state(self):
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2)

    def is_installed(self, mod_id: str) -> bool:
        return mod_id in self._state["installed"]

    # ------------------------------------------------------------------
    # Discovery and startup loading

    def _discover_candidates(self) -> dict:
        candidates = {}
        if not os.path.exists(self.modules_dir):
            return candidates
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
        return candidates

    def load_all_modules(self):
        self.loaded_modules = {}
        self.load_errors = {}
        self.discovered = {}

        if not os.path.exists(self.modules_dir):
            logger.warning(f"Modules directory not found: {self.modules_dir}")
            return

        self.discovered = self._discover_candidates()

        # Drop state entries whose folders vanished (manual deletion) so the
        # state file never accumulates ghosts.
        pruned_disabled = [m for m in self._state["disabled"] if m in self.discovered]
        pruned_installed = [m for m in self._state["installed"] if m in self.discovered]
        if pruned_disabled != self._state["disabled"] or pruned_installed != self._state["installed"]:
            self._state["disabled"] = pruned_disabled
            self._state["installed"] = pruned_installed
            self._save_state()

        disabled_set = set(self._state["disabled"])
        enabled_candidates = {
            mod_id: candidate for mod_id, candidate in self.discovered.items()
            if mod_id not in disabled_set
        }

        for candidate in self._resolve_load_order(enabled_candidates):
            self._load_module_backend(candidate)

        # Enabled but not loaded means the loader skipped it (missing/cyclic
        # dependency, exec failure). Record a generic reason if the loader
        # didn't leave a specific one, so the manager UI can say *something*.
        for mod_id in enabled_candidates:
            if mod_id not in self.loaded_modules:
                self.load_errors.setdefault(
                    mod_id, "Not loaded: missing or cyclic dependency, or backend failed to import."
                )

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
                    self.load_errors[module_id] = f"Not loaded: requires module '{dependency_id}', which is missing or disabled."
                    state[module_id] = "skipped"
                    return False

                if dependency_id in stack or state.get(dependency_id) == "visiting":
                    cycle_start = stack.index(dependency_id) if dependency_id in stack else 0
                    cycle = stack[cycle_start:] + [module_id]
                    logger.error(f"Skipping cyclic module dependencies: {' -> '.join(cycle)} -> {dependency_id}")
                    for skipped_id in set(cycle):
                        state[skipped_id] = "skipped"
                        self.load_errors[skipped_id] = f"Not loaded: cyclic dependency chain {' -> '.join(cycle)} -> {dependency_id}."
                    return False

                if not visit(dependency_id, stack + [module_id]):
                    logger.error(f"Skipping {module_id}: dependency '{dependency_id}' could not be loaded.")
                    self.load_errors[module_id] = f"Not loaded: dependency '{dependency_id}' failed to load."
                    state[module_id] = "skipped"
                    return False

            state[module_id] = "loaded"
            ordered.append(candidates[module_id])
            return True

        for module_id in sorted(candidates):
            if state.get(module_id) is None:
                visit(module_id, [])

        return ordered

    def _load_module_backend(self, candidate: dict) -> bool:
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
                self.load_errors.pop(manifest["id"], None)
                print(f"[Registry] Loaded module: {manifest.get('name', mod_name)}")
                return True
            except Exception as e:
                logger.error(f"Failed to execute module {mod_name}: {e}")
                self.load_errors[manifest["id"]] = f"Backend failed to import: {e}"
        return False

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

        if not isinstance(manifest.get("dedicated_reader", False), bool):
            raise ManifestValidationError("dedicated_reader must be a boolean.")

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

    # ------------------------------------------------------------------
    # Module manager operations (app-wide enable / disable / install / remove)

    def manager_listing(self) -> list[dict]:
        """Every discovered module with its app-wide manager state, for the
        Module Manager UI. Includes modules that are disabled or failed to
        load — unlike `get_modules()`, which is only the live set."""
        disabled = set(self._state["disabled"])
        installed = set(self._state["installed"])
        entries = []
        for mod_id, candidate in sorted(self.discovered.items()):
            manifest = candidate["manifest"]
            enabled = mod_id not in disabled
            entries.append({
                "id": mod_id,
                "name": manifest.get("name", mod_id),
                "version": manifest.get("version", ""),
                "description": manifest.get("description", ""),
                "icon": manifest.get("icon"),
                "enabled": enabled,
                "loaded": mod_id in self.loaded_modules,
                "builtin": mod_id not in installed,
                "dependencies": manifest.get("dependencies", []),
                "dependents": self._dependents_of(mod_id),
                "load_error": self.load_errors.get(mod_id) if enabled else None,
            })
        return entries

    def _dependents_of(self, mod_id: str, loaded_only: bool = False) -> list[str]:
        pool = self.loaded_modules if loaded_only else self.discovered
        return sorted(
            other_id for other_id, data in pool.items()
            if mod_id in data["manifest"].get("dependencies", [])
        )

    @staticmethod
    def _dependents_phrase(dependents: list[str]) -> str:
        names = ", ".join(repr(d) for d in dependents)
        return f"{names} {'depends' if len(dependents) == 1 else 'depend'} on it"

    def enable_module(self, mod_id: str) -> dict:
        """Enable a module app-wide and load its backend if needed. Returns the
        loaded module entry so the caller can mount its router / inject services."""
        candidate = self.discovered.get(mod_id)
        if candidate is None:
            raise ModuleManagerError(f"Module '{mod_id}' not found.", status=404)

        missing = [
            dep for dep in candidate["manifest"].get("dependencies", [])
            if dep not in self.loaded_modules
        ]
        if missing:
            raise ModuleManagerError(
                f"Cannot enable '{mod_id}': it requires {', '.join(repr(d) for d in missing)} to be enabled first.",
                status=409,
            )

        if mod_id not in self.loaded_modules:
            if not self._load_module_backend(candidate):
                raise ModuleManagerError(
                    f"Cannot enable '{mod_id}': {self.load_errors.get(mod_id, 'backend failed to import.')}"
                )

        if mod_id in self._state["disabled"]:
            self._state["disabled"].remove(mod_id)
            self._save_state()
        return self.loaded_modules[mod_id]

    def disable_module(self, mod_id: str) -> None:
        """Disable a module app-wide, immediately removing it from the live set.
        The engine stops dispatching to it on the next turn; its already-mounted
        HTTP routes linger until restart but the UI no longer reaches them."""
        if mod_id not in self.discovered:
            raise ModuleManagerError(f"Module '{mod_id}' not found.", status=404)

        loaded_dependents = self._dependents_of(mod_id, loaded_only=True)
        if loaded_dependents:
            raise ModuleManagerError(
                f"Cannot disable '{mod_id}': {self._dependents_phrase(loaded_dependents)}. Disable {'it' if len(loaded_dependents) == 1 else 'them'} first.",
                status=409,
            )

        self.loaded_modules.pop(mod_id, None)
        self.load_errors.pop(mod_id, None)
        if mod_id not in self._state["disabled"]:
            self._state["disabled"].append(mod_id)
            self._save_state()

    def install_module_from_zip(self, zip_bytes: bytes, subpath: str = None) -> dict:
        """Install a module from zip archive bytes (an upload or a downloaded
        GitHub archive), then enable it. The archive is unpacked to a temp dir,
        the module root located (manifest.json + backend.py, optionally under
        `subpath`), the manifest validated, and only then is the folder copied
        into the modules dir under the module id. Any failure rolls back
        completely. Returns the loaded module entry."""
        temp_dir = self._safe_extract_zip(zip_bytes)
        try:
            root = self._locate_module_root(temp_dir, subpath)
            manifest = self._validated_archive_manifest(root)
            mod_id = manifest["id"]

            if mod_id in self.discovered:
                raise ModuleManagerError(
                    f"Module '{mod_id}' already exists. Remove it first to reinstall.", status=409
                )
            dest = os.path.join(self.modules_dir, mod_id)
            if os.path.exists(dest):
                raise ModuleManagerError(
                    f"Folder '{mod_id}' already exists in the modules directory.", status=409
                )

            os.makedirs(self.modules_dir, exist_ok=True)
            shutil.copytree(root, dest)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        candidate = {"mod_name": mod_id, "path": dest, "manifest": manifest}
        self.discovered[mod_id] = candidate
        self._state["installed"].append(mod_id)
        self._save_state()

        try:
            return self.enable_module(mod_id)
        except ModuleManagerError:
            # A module that can't load (broken backend, missing dependency)
            # leaves no residue: full rollback so a corrected re-install works.
            self.discovered.pop(mod_id, None)
            self.load_errors.pop(mod_id, None)
            if mod_id in self._state["installed"]:
                self._state["installed"].remove(mod_id)
            self._save_state()
            shutil.rmtree(dest, ignore_errors=True)
            raise

    def remove_module(self, mod_id: str) -> None:
        """Delete a manager-installed module from disk. Built-in modules
        (anything not installed through the manager) can only be disabled."""
        candidate = self.discovered.get(mod_id)
        if candidate is None:
            raise ModuleManagerError(f"Module '{mod_id}' not found.", status=404)
        if mod_id not in self._state["installed"]:
            raise ModuleManagerError(
                f"Module '{mod_id}' is built-in and cannot be removed. Disable it instead.", status=403
            )

        loaded_dependents = self._dependents_of(mod_id, loaded_only=True)
        if loaded_dependents:
            raise ModuleManagerError(
                f"Cannot remove '{mod_id}': {self._dependents_phrase(loaded_dependents)}. Disable {'it' if len(loaded_dependents) == 1 else 'them'} first.",
                status=409,
            )

        self.loaded_modules.pop(mod_id, None)
        self.load_errors.pop(mod_id, None)
        self.discovered.pop(mod_id, None)
        shutil.rmtree(candidate["path"], ignore_errors=True)

        changed = False
        for key in ("disabled", "installed"):
            if mod_id in self._state[key]:
                self._state[key].remove(mod_id)
                changed = True
        if changed:
            self._save_state()

    # ------------------------------------------------------------------
    # Archive helpers

    def _safe_extract_zip(self, zip_bytes: bytes) -> str:
        """Extract archive bytes to a fresh temp dir, rejecting path traversal,
        absolute paths, symlink members, and oversized payloads."""
        try:
            archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile:
            raise ModuleManagerError("Not a valid zip archive.")

        total_size = sum(info.file_size for info in archive.infolist())
        if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ModuleManagerError(
                f"Archive too large: {total_size // (1024 * 1024)} MB uncompressed exceeds the "
                f"{MAX_ARCHIVE_UNCOMPRESSED_BYTES // (1024 * 1024)} MB limit."
            )

        temp_dir = tempfile.mkdtemp(prefix="wb_module_install_")
        try:
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                parts = name.split("/")
                is_absolute = name.startswith("/") or (len(name) > 1 and name[1] == ":")
                if is_absolute or ".." in parts:
                    raise ModuleManagerError(f"Archive contains an unsafe path: {info.filename}")
                # Symlink members could point outside the module dir, dodging
                # the traversal check — skip them outright.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue
                archive.extract(info, temp_dir)
        except ModuleManagerError:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        return temp_dir

    def _locate_module_root(self, extracted_dir: str, subpath: str = None) -> str:
        """Find the single folder holding manifest.json + backend.py inside an
        extracted archive. GitHub zips wrap everything in `repo-branch/`, so a
        given subpath is tried both from the archive root and inside a sole
        top-level folder."""
        def is_module_root(path: str) -> bool:
            return (os.path.isfile(os.path.join(path, "manifest.json"))
                    and os.path.isfile(os.path.join(path, "backend.py")))

        if subpath:
            sub_parts = [p for p in subpath.split("/") if p]
            if ".." in sub_parts:
                raise ModuleManagerError("Invalid subpath in URL.")
            bases = [os.path.join(extracted_dir, *sub_parts)]
            top_entries = [e for e in os.listdir(extracted_dir)
                           if os.path.isdir(os.path.join(extracted_dir, e))]
            if len(top_entries) == 1:
                bases.append(os.path.join(extracted_dir, top_entries[0], *sub_parts))
            for base in bases:
                if is_module_root(base):
                    return base
            raise ModuleManagerError(
                f"No module (manifest.json + backend.py) found at '{subpath}' in the repository."
            )

        roots = []
        queue = [(extracted_dir, 0)]
        while queue:
            current, depth = queue.pop(0)
            if is_module_root(current):
                roots.append(current)
                continue  # a module root's subfolders are its own business
            if depth >= 3:
                continue
            for entry in sorted(os.listdir(current)):
                child = os.path.join(current, entry)
                if os.path.isdir(child) and entry not in _ARCHIVE_SCAN_IGNORE and not entry.startswith("."):
                    queue.append((child, depth + 1))

        if not roots:
            raise ModuleManagerError(
                "No module found in the archive. A module needs manifest.json and backend.py in the same folder."
            )
        if len(roots) > 1:
            rels = ", ".join(sorted(os.path.relpath(r, extracted_dir) for r in roots))
            raise ModuleManagerError(
                f"Multiple modules found in the archive ({rels}). "
                "Link directly to one module folder (e.g. a GitHub /tree/branch/path URL)."
            )
        return roots[0]

    def _validated_archive_manifest(self, root: str) -> dict:
        manifest_path = os.path.join(root, "manifest.json")
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except json.JSONDecodeError as e:
            raise ModuleManagerError(f"manifest.json is not valid JSON: {e}")
        try:
            self._validate_manifest(manifest, os.path.basename(root))
        except ManifestValidationError as e:
            raise ModuleManagerError(f"Invalid manifest: {e}")
        return manifest
