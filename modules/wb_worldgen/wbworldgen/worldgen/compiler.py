"""World compilation: merging step outputs into the game-ready world dict.

These are pure functions operating on the plain ``world_state`` dict so they
can be reused by the map generator, the facade, persistence and tests without
depending on the orchestrator.
"""

from typing import Any, Optional


def merge_geography_steps(steps_data: dict) -> dict:
    """Merge terrain_regions + natural_landmarks + society_factions into a
    single unified region list."""
    terrain_data = steps_data.get("terrain_regions", {}).get("data", {})
    landmarks_data = steps_data.get("natural_landmarks", {}).get("data", {})
    society_data = steps_data.get("society_factions", {}).get("data", {})

    region_list = terrain_data.get("regions", [])
    all_landmarks = landmarks_data.get("landmarks", [])
    all_factions = society_data.get("factions", [])

    merged_regions = []
    for region in region_list:
        rname = region.get("name", "")
        rlayer = region.get("layer_id", "")

        # ``named_locations`` carries the authored entities (with descriptions)
        # so they can be placed onto actual map nodes. ``landmarks``/``factions``
        # remain bare name lists for backward compatibility + enrichment context.
        named_locations: list[dict] = []
        seen_names: set[str] = set()

        def _add_location(name: str, category: str, description: str = "",
                          environment: str = ""):
            name = (name or "").strip()
            if not name:
                return
            key = name.lower()
            if key in seen_names:
                return
            seen_names.add(key)
            loc = {
                "name": name,
                "category": category,
                "description": description or "",
            }
            if environment:
                loc["environment"] = environment
            named_locations.append(loc)

        natural_lm_names = []
        for lm in all_landmarks:
            if lm.get("region") == rname and (not rlayer or lm.get("layer_id") == rlayer):
                lm_name = lm.get("name", "")
                natural_lm_names.append(lm_name)
                _add_location(lm_name, "landmark", lm.get("description", ""),
                              environment=lm.get("environment", ""))

        region_factions = []
        faction_details = []
        society_lm_names = []
        for faction in all_factions:
            if faction.get("region") == rname and (not rlayer or faction.get("layer_id") == rlayer):
                fname = faction.get("name", "")
                region_factions.append(fname)
                # Preserve full faction data for RAG embedding (name-only list kept for compat).
                faction_details.append({
                    "name": fname,
                    "type": faction.get("type", ""),
                    "description": faction.get("description", ""),
                    "settlements": faction.get("settlements", []),
                })
                for settlement in faction.get("settlements", []):
                    # No placeholder description: a non-empty description here
                    # would be bound onto the map node and make the node_descriptions
                    # enrichment step treat it as already-described, permanently
                    # skipping the real flavor text it's supposed to generate.
                    _add_location(settlement, "settlement", "")
                for slm in faction.get("significant_landmarks", []):
                    society_lm_names.append(slm)
                    _add_location(slm, "landmark", "")

        merged_regions.append({
            "name": rname,
            "layer_id": rlayer,
            "terrain": region.get("terrain", ""),
            "climate": region.get("climate", ""),
            "description": region.get("description", ""),
            "landmarks": natural_lm_names + society_lm_names,
            "factions": region_factions,
            "faction_details": faction_details,
            "named_locations": named_locations,
        })

    return {"regions": merged_regions}


def collect_scope_content(steps_data: dict) -> dict:
    """Landmarks/factions attached to hierarchy scopes (world_format 2).

    Returns {scope_label: {"landmarks": [...], "factions": [...],
    "named_locations": [...]}} where scope_label "" is the root/world map.
    Same dedup and no-placeholder-description rules as the legacy region
    merge (a non-empty settlement description would make node_descriptions
    treat the node as already described)."""
    landmarks_data = steps_data.get("natural_landmarks", {}).get("data", {})
    society_data = steps_data.get("society_factions", {}).get("data", {})

    scopes: dict[str, dict] = {}

    def _scope(label: str) -> dict:
        key = (label or "").strip()
        if key not in scopes:
            scopes[key] = {"landmarks": [], "factions": [],
                           "named_locations": [], "_seen": set()}
        return scopes[key]

    def _add_location(scope: dict, name: str, category: str,
                      description: str = "", environment: str = ""):
        name = (name or "").strip()
        if not name or name.lower() in scope["_seen"]:
            return
        scope["_seen"].add(name.lower())
        loc = {"name": name, "category": category, "description": description or ""}
        if environment:
            loc["environment"] = environment
        scope["named_locations"].append(loc)

    for lm in landmarks_data.get("landmarks", []) or []:
        scope = _scope(lm.get("scope", ""))
        scope["landmarks"].append({
            "name": lm.get("name", ""),
            "type": lm.get("type", ""),
            "description": lm.get("description", ""),
            "environment": lm.get("environment", ""),
        })
        _add_location(scope, lm.get("name", ""), "landmark",
                      lm.get("description", ""), lm.get("environment", ""))

    for faction in society_data.get("factions", []) or []:
        scope = _scope(faction.get("scope", ""))
        scope["factions"].append({
            "name": faction.get("name", ""),
            "type": faction.get("type", ""),
            "description": faction.get("description", ""),
            "settlements": faction.get("settlements", []),
        })
        for settlement in faction.get("settlements", []) or []:
            _add_location(scope, settlement, "settlement", "")
        for slm in faction.get("significant_landmarks", []) or []:
            scope["landmarks"].append({"name": slm, "type": "landmark",
                                       "description": "", "environment": ""})
            _add_location(scope, slm, "landmark", "")

    for scope in scopes.values():
        scope.pop("_seen", None)
    return scopes


def attach_scope_content(compiled: dict, steps_data: dict):
    """Attach scope landmarks/factions onto the compiled MapRecords by label
    (empty/unmatched scopes go to the root map). No-op when the steps carry
    legacy region-keyed data (merge_geography_steps handles those)."""
    maps = compiled.get("maps")
    if not isinstance(maps, dict) or not maps:
        return
    scopes = collect_scope_content(steps_data)
    if not scopes:
        return
    root_id = compiled.get("root_map_id", "root")
    by_label = {str(m.get("label", "")).strip().lower(): mid
                for mid, m in maps.items()}
    for label, content in scopes.items():
        map_id = by_label.get(label.strip().lower()) if label else root_id
        record = maps.get(map_id) or maps.get(root_id)
        if record is None:
            continue
        if content["landmarks"]:
            record.setdefault("landmarks", []).extend(content["landmarks"])
        if content["factions"]:
            record.setdefault("factions", []).extend(content["factions"])


def build_compiled_for_map(world_state: dict) -> dict:
    """Lightweight compile used as input to procedural map generation."""
    steps = world_state.get("steps", {})
    rules_data = steps.get("world_rules", {}).get("data", {})
    lore_data = steps.get("lore", {}).get("data", {})
    terrain_data = steps.get("terrain_generation", {}).get("data", {})
    return {
        "generated_from": world_state.get("seed_prompt", ""),
        "rules": rules_data if isinstance(rules_data, dict) else {},
        "lore": lore_data if isinstance(lore_data, dict) else {},
        "regions": merge_geography_steps(steps),
        "terrain": terrain_data if isinstance(terrain_data, dict) else {},
        "world_id": terrain_data.get("world_id", "") if isinstance(terrain_data, dict) else "",
    }


def compile_world(world_state: dict, steps: Optional[dict] = None) -> dict:
    """Merge all step outputs into a single game-ready dict.

    The base merge knows the canonical step ids (and tolerates any of them
    being absent). If ``steps`` (the registered step objects) is provided, each
    step that defines ``contribute_to_compiled(steps_data, compiled)`` is given
    a chance to extend the result -- this is how brand-new custom steps fold
    their data in without editing this module.
    """
    steps_data = world_state.get("steps", {})
    compiled: dict[str, Any] = {
        "rules": steps_data.get("world_rules", {}).get("data", {}),
        "lore": steps_data.get("lore", {}).get("data", {}),
        "regions": merge_geography_steps(steps_data),
        "generated_from": world_state.get("seed_prompt", ""),
    }
    if world_state.get("scenario"):
        compiled["scenario"] = world_state["scenario"]

    rules_data = steps_data.get("world_rules", {}).get("data", {})
    if isinstance(rules_data, dict) and rules_data.get("module_data"):
        compiled["module_data"] = rules_data["module_data"]

    layer_data = steps_data.get("layer_design", {}).get("data", {})
    if isinstance(layer_data, dict) and layer_data.get("layers"):
        compiled["layers"] = layer_data.get("layers", [])

    layer_rules_data = steps_data.get("layer_rules", {}).get("data", {})
    if isinstance(layer_rules_data, dict):
        compiled["layer_rules"] = layer_rules_data.get("layer_rules", [])
        compiled["layer_global_rules"] = layer_rules_data.get("world_rules", [])

    map_step = steps_data.get("map_generation", {})
    map_data = map_step.get("data", {}) if map_step else {}
    if isinstance(map_data, dict):
        if "layers" in map_data:
            compiled["map_layers"] = map_data.get("layers", [])
            compiled["map_connections"] = map_data.get("connections", [])
        elif "nodes" in map_data:
            compiled["map"] = map_data

    # Lazily-expanded interior detail (site bundles), keyed by parent node id.
    # Additive: worlds without sites simply lack the key.
    sites = world_state.get("sites")
    if isinstance(sites, dict) and sites:
        compiled["site_maps"] = sites

    # World template identity + vocabulary snapshot (additive; absent for
    # pre-template worlds, which behave as the default fantasy template).
    if world_state.get("template_id"):
        compiled["template_id"] = world_state["template_id"]
    if isinstance(world_state.get("template_vocab"), dict) and world_state["template_vocab"]:
        compiled["template_vocab"] = world_state["template_vocab"]

    # Optional per-step contributions (for custom/extension steps).
    for step in (steps or {}).values():
        contribute = getattr(step, "contribute_to_compiled", None)
        if callable(contribute):
            try:
                contribute(steps_data, compiled)
            except Exception:
                pass

    # Compiled worlds are always world_format 2: legacy flat/layered map data
    # (from old step data) is migrated into the hierarchical maps+connections
    # shape here, so every downstream reader sees one format only.
    # Template-declared hierarchy levels (free text) ride into the compiled
    # world; migrate fills the default [world, interior] when absent.
    if world_state.get("hierarchy_levels"):
        compiled["hierarchy"] = {
            "levels": world_state["hierarchy_levels"],
            "notes": steps_data.get("hierarchy_design", {}).get("data", {}).get("notes", ""),
        }

    from .migrate import migrate_world_data
    compiled = migrate_world_data(compiled)
    attach_scope_content(compiled, steps_data)

    # Fold in lazily-expanded child maps (write-once cache under maps/).
    for bundle in world_state.get("child_maps", []) or []:
        record = bundle.get("map") or {}
        if record.get("map_id") and record["map_id"] not in compiled.get("maps", {}):
            compiled.setdefault("maps", {})[record["map_id"]] = record
            existing_ids = {c.get("id") for c in compiled.setdefault("connections", [])}
            compiled["connections"].extend(
                c for c in bundle.get("connections", []) if c.get("id") not in existing_ids)
    return compiled
