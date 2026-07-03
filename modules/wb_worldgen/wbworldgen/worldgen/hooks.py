"""Module hook registration + dispatch for world-building events."""

import logging

logger = logging.getLogger(__name__)

HOOK_NAMES = [
    "on_world_rules_schema",
    "on_world_rules_generate",
    "on_region_generate",
    "on_faction_generate",
    "on_world_compiled",
]


class HookRegistry:
    def __init__(self):
        self._hooks: dict[str, list] = {name: [] for name in HOOK_NAMES}

    @property
    def hooks(self) -> dict:
        return self._hooks

    def register_from_modules(self, registry):
        for mod_id, mod_entry in registry.loaded_modules.items():
            backend = mod_entry.get("backend")
            if backend is None:
                continue
            for hook_name in self._hooks:
                if hasattr(backend, hook_name):
                    self._hooks[hook_name].append((mod_id, getattr(backend, hook_name)))
        if any(self._hooks.values()):
            hooked = {k: len(v) for k, v in self._hooks.items() if v}
            logger.info("World builder module hooks registered: %s", hooked)

    async def dispatch_step(self, step_id: str, data: dict, world_state: dict, user_prompt: str):
        """Fire post-generation hooks for a freshly generated step."""
        if step_id == "world_rules":
            for mod_id, hook in self._hooks.get("on_world_rules_generate", []):
                try:
                    module_data = await hook(user_prompt, data, None)
                    if isinstance(module_data, dict):
                        data.setdefault("module_data", {})
                        data["module_data"][mod_id] = module_data
                except Exception as e:
                    logger.warning("Module %s on_world_rules_generate failed: %s", mod_id, e)
        elif step_id == "terrain_regions":
            for region in data.get("regions", []):
                for mod_id, hook in self._hooks.get("on_region_generate", []):
                    try:
                        module_data = await hook(region, world_state, None)
                        if isinstance(module_data, dict):
                            region.setdefault("module_data", {})
                            region["module_data"][mod_id] = module_data
                    except Exception as e:
                        logger.warning("Module %s on_region_generate failed: %s", mod_id, e)
        elif step_id == "society_factions":
            for faction in data.get("factions", []):
                for mod_id, hook in self._hooks.get("on_faction_generate", []):
                    try:
                        module_data = await hook(faction, world_state, None)
                        if isinstance(module_data, dict):
                            faction.setdefault("module_data", {})
                            faction["module_data"][mod_id] = module_data
                    except Exception as e:
                        logger.warning("Module %s on_faction_generate failed: %s", mod_id, e)
