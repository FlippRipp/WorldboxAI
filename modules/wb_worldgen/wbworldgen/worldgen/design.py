"""One query surface over the AI's world design.

The ``world_form`` and ``hierarchy_design`` steps PRODUCE per-world design
data: what kind of world this is, its root map style, per-step coverage
directives, which optional steps to skip, and the map hierarchy (levels,
parallel planes, pregenerate plans). Everything that READS those decisions —
the facade, the compiler, the map generators, the expansion engines, other
steps — asks this module instead of reaching into the step modules, so
"what did the AI decide for this world" has exactly one home and a new
level type or generator touches the registry plus, at most, this file.

Every function takes the raw ``world_state`` dict and degrades to the
pre-design-era behavior when the data is absent (old worlds, seeded worlds):
empty skips, empty directives, default levels, procedural root map.
"""

#: Steps the AI may skip outright. Everything else is structural: world_rules /
#: lore / hierarchy_design / map_generation feed the compiled contract (world
#: ids, hierarchy, the map itself) and enrichment steps are engine-driven.
#: terrain_generation is controlled by ``map_style``, not listed here.
AI_SKIPPABLE = {"codex", "natural_landmarks", "society_factions"}

#: The generator a root map style pins the FIRST hierarchy level to
#: ("abstract" pins nothing — the level's own generator stands, drawing a
#: clean node graph when it is world_map without terrain).
STYLE_ROOT_GENERATORS = {"terrain": "world_map", "city": "city_roadnet"}


def _world_form_data(world_state: dict) -> dict:
    data = ((world_state or {}).get("steps", {}).get("world_form", {}) or {}).get("data")
    return data if isinstance(data, dict) else {}


def _hierarchy_data(world_state: dict) -> dict:
    data = ((world_state or {}).get("steps", {}).get("hierarchy_design", {}) or {}).get("data")
    return data if isinstance(data, dict) else {}


# --- world_form reads (the design pass's pipeline-shaping decisions) --------

def dynamic_skips(world_state: dict) -> set:
    """Step ids the world's own design turns off. Empty when no world_form
    data exists (old worlds, seeded worlds) so everything else behaves exactly
    as before the design step existed."""
    data = _world_form_data(world_state)
    if not data:
        return set()
    skips = {s for s in (data.get("skip_steps") or []) if s in AI_SKIPPABLE}
    if data.get("map_style") in ("abstract", "city"):
        skips.add("terrain_generation")
    return skips


def world_kind(world_state: dict) -> str:
    """The design's one-line reading of what this world is ("" when absent).
    The facade appends it to the system framing, giving every later
    generation call a per-world genre voice."""
    return str(_world_form_data(world_state).get("world_kind") or "").strip()


def map_style(world_state: dict) -> str:
    """The design's root map style ("" when absent — old worlds)."""
    return str(_world_form_data(world_state).get("map_style") or "").strip()


def aligned_root_generator(style: str) -> str:
    """The generator id a map style pins the root level to ("" when the
    style imposes nothing)."""
    return STYLE_ROOT_GENERATORS.get(str(style or "").strip(), "")


def map_generator_override(world_state: dict) -> str:
    """Generator id the world's own design imposes on the root map ("" when
    the level default should stand). A "city" map style routes map
    generation to the street-network generator; "terrain" imposes nothing
    because world_map already is the default."""
    if map_style(world_state) == "city":
        return STYLE_ROOT_GENERATORS["city"]
    return ""


def coverage_directive(world_state: dict, step_id: str) -> str:
    """The world-design directive for one step ("" when none)."""
    for entry in _world_form_data(world_state).get("step_directives") or []:
        if isinstance(entry, dict) and entry.get("step_id") == step_id:
            return str(entry.get("directive") or "").strip()
    return ""


# --- hierarchy_design reads (the AI-authored map structure) -----------------

def designed_levels(world_state: dict) -> list:
    """The world's own AI-authored hierarchy levels ([] when absent — old
    worlds and pre-design step data keep the default levels)."""
    levels = _hierarchy_data(world_state).get("levels")
    if not isinstance(levels, list):
        return []
    out = []
    for level in levels:
        if not isinstance(level, dict):
            continue
        if not level.get("level_type") or not level.get("generator_id"):
            continue
        entry = dict(level)
        if entry["generator_id"] == "interior":
            # Player-added form entries have no nestable flag; interior-style
            # levels always nest (a vault inside a castle).
            entry.setdefault("nestable", True)
        out.append(entry)
    return out


def parallel_maps(world_state: dict) -> list:
    """The designed parallel planes beside the root map ([] when none)."""
    return [p for p in _hierarchy_data(world_state).get("parallel_maps") or []
            if isinstance(p, dict) and str(p.get("label", "")).strip()]


def pregenerate_plans(world_state: dict) -> list:
    """The named locations whose sub-maps the design wants built at world
    creation ([] when none)."""
    return [p for p in _hierarchy_data(world_state).get("pregenerate") or []
            if isinstance(p, dict) and str(p.get("location_name", "")).strip()]


# --- root-map resolution (which generator draws the world's root map, and
# whether that root is authored rather than procedural) ----------------------

def root_generator_for(world_state: dict) -> str:
    """The generator that draws a world's root map. The world's own designed
    structure (hierarchy_design levels) is authoritative when present; worlds
    without one (old worlds, junk design output) fall back to the world_form
    "city" override over the default overworld generator, exactly as before
    the structure step existed."""
    designed = designed_levels(world_state)
    if designed:
        return designed[0].get("generator_id") or "world_map"
    return map_generator_override(world_state) or "world_map"


def authored_root_level(world_state: dict, root_gen: str = None) -> dict | None:
    """The designed root level when its generator needs authored (LLM)
    content — a world whose whole playable space is one interior-style
    map. None for procedural roots (terrain, abstract, city)."""
    from wbworldgen.worldgen.generation.registry import GENERATOR_REGISTRY
    if root_gen is None:
        root_gen = root_generator_for(world_state)
    spec = GENERATOR_REGISTRY.get(root_gen)
    if spec is None or not spec.needs_llm_content:
        return None
    designed = designed_levels(world_state)
    if designed:
        return designed[0]
    return {"level_type": "interior", "label": "Interior", "generator_id": root_gen}


def abstract_root_level(world_state: dict, root_gen: str = None) -> dict | None:
    """The designed root level when the world's own design declared the
    map abstract — a conceptual graph of places (a solar system, a dream
    web) with no procedural landscape. The root map is then AUTHORED by
    an LLM call reading the hierarchy guidance and map directive instead
    of scattered procedurally. None for terrain/city roots,
    terrain-flagged root levels, and worlds predating the design step
    (their maps stay procedural, exactly as before)."""
    if root_gen is None:
        root_gen = root_generator_for(world_state)
    if root_gen != "world_map":
        return None
    if map_style(world_state) != "abstract":
        return None
    designed = designed_levels(world_state)
    level = designed[0] if designed else {
        "level_type": "world", "label": "World", "generator_id": "world_map"}
    if level.get("terrain"):
        return None
    return level
