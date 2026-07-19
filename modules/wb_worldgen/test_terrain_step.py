"""The terrain step's layer resolution contract.

Modern (hierarchy-era) worlds get exactly one raster — the root map's,
keyed ``main`` — no matter how many parallel or child maps the hierarchy
designs; terrain-flagged children are rasterized at expansion time instead.
Only data from the deprecated ``layer_design`` step (pre-hierarchy worlds)
reaches the multi-layer branch. Pinned after a live agent build looped on
re-running the step expecting per-planet layers that cannot exist.
"""

from wbworldgen.worldgen.steps.terrain_generation import _layer_specs

_ROOT_ONLY = [{"layer_id": "main", "name": "Overworld",
               "layer_type": "surface", "index": 0}]


def test_empty_world_gets_single_root_layer():
    assert _layer_specs({}) == _ROOT_ONLY


def test_hierarchy_world_with_parallel_maps_gets_single_root_layer():
    # The Crucible Stars shape: an abstract system root plus three parallel
    # planet maps. The step must NOT invent per-planet layers.
    state = {"steps": {"hierarchy_design": {"data": {
        "levels": [
            {"level_type": "system_map", "generator_id": "world_map"},
            {"level_type": "planet", "generator_id": "world_map",
             "terrain": True},
        ],
        "parallel_maps": [
            {"label": "Venus-M", "level_type": "planet"},
            {"label": "Eros-3", "level_type": "planet"},
            {"label": "Slimehome", "level_type": "planet"},
        ],
    }}}}
    assert _layer_specs(state) == _ROOT_ONLY


def test_legacy_layer_design_data_still_drives_multilayer():
    # Pre-hierarchy worlds carrying deprecated layer_design data keep their
    # one-raster-per-layer rebuild path.
    state = {"steps": {"layer_design": {"data": {
        "has_multiple_layers": True,
        "layers": [
            {"layer_id": "overworld", "name": "Overworld", "index": 0},
            {"layer_id": "underground", "name": "The Underdark",
             "layer_type": "underground", "index": 1},
        ],
    }}}}
    assert [s["layer_id"] for s in _layer_specs(state)] == [
        "overworld", "underground"]
