"""Interior layout tests: determinism, connectivity bridging, entrance
pinning and travel-compatible output.

Run by explicit path: python -m pytest modules/wb_worldgen/test_interior_layout.py
"""

from wbworldgen.worldgen.generation.interior_layout import MAP_SIZE, layout_interior
from wbworldgen.worldgen.generation.registry import (
    GENERATOR_REGISTRY,
    get_generator,
    list_generators,
)

import pytest


def _rooms():
    return [
        {"name": "Gatehouse", "type": "gate", "description": "The way in.",
         "adjacent": ["Courtyard"], "is_entrance": True},
        {"name": "Courtyard", "type": "court", "adjacent": ["Gatehouse", "Great Hall"]},
        {"name": "Great Hall", "type": "hall", "adjacent": ["Courtyard"]},
        # Disconnected pair -> must be bridged.
        {"name": "Crypt", "type": "crypt", "adjacent": ["Ossuary"]},
        {"name": "Ossuary", "type": "crypt", "adjacent": ["Crypt"]},
    ]


def test_layout_is_deterministic_and_complete():
    a = layout_interior("m_x", _rooms())
    b = layout_interior("m_x", _rooms())
    assert a == b
    assert len(a["nodes"]) == 5
    assert all("x" in n and "y" in n and n["importance"] >= 1 for n in a["nodes"])
    assert a["config"]["instant_travel"] is True


def test_disconnected_components_are_bridged():
    result = layout_interior("m_x", _rooms())
    # 3 authored adjacency pairs + 1 bridge.
    assert len(result["edges"]) == 4
    ids = {n["id"] for n in result["nodes"]}
    for e in result["edges"]:
        assert e["from"] in ids and e["to"] in ids
        assert e["distance"] >= 1.0


def test_entrance_is_pinned_to_the_bottom():
    result = layout_interior("m_x", _rooms())
    entrance = next(n for n in result["nodes"] if n["name"] == "Gatehouse")
    assert result["entrance_node_id"] == entrance["id"]
    assert entrance["y"] >= MAP_SIZE * 0.85


def test_ids_default_to_map_namespace_but_keep_given_ids():
    rooms = _rooms()
    rooms[0]["id"] = "c1:s1"  # migrated site sub id
    result = layout_interior("m_x", rooms)
    assert result["nodes"][0]["id"] == "c1:s1"
    assert result["nodes"][1]["id"] == "m_x:n1"


def test_empty_locations_yield_empty_map():
    result = layout_interior("m_x", [])
    assert result["nodes"] == [] and result["edges"] == []


def test_generator_registry_contract():
    ids = {g["id"] for g in list_generators()}
    assert {"world_map", "interior", "region", "star_system"} <= ids
    assert get_generator("interior").needs_llm_content
    with pytest.raises(KeyError):
        get_generator("nonsense")
    with pytest.raises(NotImplementedError):
        get_generator("star_system")
    # Reserved stubs are registered but explicitly unimplemented.
    assert GENERATOR_REGISTRY["region"].build is None
