"""Labeled map-generator registry.

Each generator produces one map of the hierarchy in the standard per-map
shape ({nodes, edges, config, ...}). Templates and the expansion engine pick
generators by id via a level's ``generator_id``; unknown or unimplemented
ids fail loudly so a template can never silently select a generator that
does not exist.

Shipped now: ``world_map`` (the procedural overworld generator),
``city_roadnet`` (planar street-network city) and ``interior``
(deterministic layout over LLM-authored rooms). ``region`` and
``star_system`` are reserved ids — registered as explicit stubs.
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class MapGeneratorSpec:
    id: str
    label: str
    description: str
    needs_llm_content: bool
    build: Callable = None  # (spec: dict) -> map dict; None = not implemented
    extras: dict = field(default_factory=dict)


GENERATOR_REGISTRY: dict[str, MapGeneratorSpec] = {}


def register_generator(spec: MapGeneratorSpec):
    GENERATOR_REGISTRY[spec.id] = spec
    return spec


def get_generator(generator_id: str) -> MapGeneratorSpec:
    spec = GENERATOR_REGISTRY.get(generator_id)
    if spec is None:
        raise KeyError(
            f"Unknown map generator '{generator_id}'. Registered: "
            f"{sorted(GENERATOR_REGISTRY)}")
    if spec.build is None:
        raise NotImplementedError(
            f"Map generator '{generator_id}' is a reserved id with no "
            f"implementation yet. Use one of: "
            f"{sorted(gid for gid, s in GENERATOR_REGISTRY.items() if s.build)}")
    return spec


def list_generators() -> list[dict]:
    return [{"id": s.id, "label": s.label, "description": s.description,
             "implemented": s.build is not None,
             "needs_llm_content": s.needs_llm_content}
            for s in GENERATOR_REGISTRY.values()]


def _build_world_map(spec: dict) -> dict:
    """Procedural overworld/terrain map (wraps WorldMapGenerator).

    spec: {compiled_world, total_nodes?, map_width?, map_height?, id_prefix?,
    terrain?, seed?}."""
    from wbworldgen.world_map import WorldMapGenerator
    gen = WorldMapGenerator(seed=spec.get("seed"))
    wm = gen.generate(
        spec["compiled_world"],
        total_nodes=spec.get("total_nodes", 100),
        map_width=spec.get("map_width", 1000),
        map_height=spec.get("map_height", 1000),
        id_prefix=spec.get("id_prefix", ""),
        terrain=spec.get("terrain"),
    )
    return wm.to_dict()


def _build_city_roadnet(spec: dict) -> dict:
    """Planar street-network city (wraps build_city_map).

    spec: {compiled_world, total_nodes?, map_width?, map_height?, seed?,
    id_prefix?}."""
    from .city_map import build_city_map
    return build_city_map(spec)


def _build_interior(spec: dict) -> dict:
    """Deterministic layout over authored rooms.

    spec: {map_id, locations: [{id?, name, type, description, adjacent,
    is_entrance?}]}."""
    from .interior_layout import layout_interior
    return layout_interior(spec["map_id"], spec.get("locations", []))


register_generator(MapGeneratorSpec(
    id="world_map",
    label="World Map",
    description="Procedural large-scale map: terrain-aware or abstract node "
                "placement, regions, roads. For worlds, planets and other "
                "wide-open scales.",
    needs_llm_content=False,
    build=_build_world_map,
))

register_generator(MapGeneratorSpec(
    id="city_roadnet",
    label="City Street Map",
    description="Planar street-network city: recursively grown avenues and "
                "streets, blocks clustered into districts, plazas and venues "
                "as playable locations.",
    needs_llm_content=False,
    build=_build_city_roadnet,
))

register_generator(MapGeneratorSpec(
    id="interior",
    label="Interior Map",
    description="Rooms, halls and courts of one building, complex or vessel — "
                "authored content laid out deterministically, entrance at the "
                "bottom.",
    needs_llm_content=True,
    build=_build_interior,
))

register_generator(MapGeneratorSpec(
    id="region",
    label="Region Map",
    description="Reserved: a mid-scale slice of a larger map (a province, a "
                "sector). Not implemented yet.",
    needs_llm_content=False,
))

register_generator(MapGeneratorSpec(
    id="star_system",
    label="Star System Map",
    description="Reserved: orbital layout of planets and stations around a "
                "star. Not implemented yet.",
    needs_llm_content=False,
))
