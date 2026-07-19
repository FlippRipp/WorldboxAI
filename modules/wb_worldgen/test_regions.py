"""Region-territory (terrain-matched, contiguous) tests.

The terrain map path now carves contiguous region territories from each region's
terrain/climate text BEFORE assigning nodes, so regions stop coming out
fragmented. Run by path with the venv python:

    venv\\Scripts\\python.exe -m pytest modules/wb_worldgen/test_regions.py -q
"""
import numpy as np
import pytest

from wbworldgen.terrain import biomes as _bm
from wbworldgen.worldgen.generation.overworld import WorldMapGenerator
from wbworldgen.worldgen import region_affinity as _ra
from wbworldgen.worldgen import terrain_placement as _tp


RES = 96


def _split_terrain(left_biome=_bm.DESERT, right_biome=_bm.FOREST, res=RES):
    """Synthetic terrain: all land, left half one biome, right half another.

    Includes every raster key the placement / roads / suitability code reads.
    """
    biome = np.full((res, res), right_biome, dtype=np.int16)
    biome[:, : res // 2] = left_biome
    height = np.full((res, res), 0.55, dtype=np.float32)
    slope = np.full((res, res), 0.1, dtype=np.float32)
    water = np.zeros((res, res), dtype=bool)  # all land
    moisture = np.full((res, res), 0.5, dtype=np.float32)
    # Desert warm/left, forest cooler/right — gives the climate keywords signal.
    temperature = np.full((res, res), 0.55, dtype=np.float32)
    temperature[:, : res // 2] = 0.8
    return {
        "height": height,
        "slope": slope,
        "water": water,
        "biome": biome,
        "moisture": moisture,
        "temperature": temperature,
        "river_mask": np.zeros((res, res), dtype=bool),
        "lake_mask": np.zeros((res, res), dtype=bool),
        "sea_level": 0.4,
    }


def _regions():
    return [
        {"name": "Desert Waste", "terrain": "arid desert of sand dunes",
         "climate": "hot and dry", "named_locations": [
             {"name": "Dusthold", "category": "settlement", "description": "A dry town."},
             {"name": "Sunspire", "category": "landmark", "environment": "desert_waste"},
         ]},
        {"name": "Greenwood", "terrain": "dense temperate forest",
         "climate": "cool and wet", "named_locations": [
             {"name": "Elmgate", "category": "settlement", "description": "A woodland town."},
         ]},
    ]


def _compiled(regions):
    return {"regions": {"regions": regions}, "generated_from": "test"}


def _connected_components(node_ids, edges):
    """Count connected components of a node-id set over the edge list."""
    idset = set(node_ids)
    adj = {nid: [] for nid in idset}
    for e in edges:
        a, b = e["from"], e["to"]
        if a in idset and b in idset:
            adj[a].append(b)
            adj[b].append(a)
    seen = set()
    comps = 0
    for start in idset:
        if start in seen:
            continue
        comps += 1
        stack = [start]
        seen.add(start)
        while stack:
            v = stack.pop()
            for nb in adj[v]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
    return comps


# ---------------------------------------------------------------------------
# Territory partition (unit)
# ---------------------------------------------------------------------------

def test_affinity_field_prefers_matching_biome():
    terrain = _split_terrain()
    fields = _tp.suitability_fields(terrain)
    desert, forest = _regions()
    aff_desert = _ra.region_affinity_field(desert, terrain, fields, res=RES)
    # Desert region should score its own (left) half higher than the forest half.
    left = aff_desert[:, : RES // 2].mean()
    right = aff_desert[:, RES // 2:].mean()
    assert left > right


def test_no_keyword_region_still_gets_uniform_land_field():
    terrain = _split_terrain()
    fields = _tp.suitability_fields(terrain)
    void = {"name": "The Void", "terrain": "mysterious void", "climate": "unknowable"}
    aff = _ra.region_affinity_field(void, terrain, fields, res=RES)
    # Uniform positive over land (all land here), never all-zero.
    assert aff.max() > 0
    assert np.all(aff > 0)


def test_partition_is_terrain_matched():
    terrain = _split_terrain()
    fields = _tp.suitability_fields(terrain)
    regions = _regions()
    grid = _ra.partition_regions(regions, terrain, fields, 1000.0, 1000.0,
                                 np.random.RandomState(0), res=RES)
    # Region 0 (desert) should own mostly the left half, region 1 the right.
    left = grid[:, : RES // 2]
    right = grid[:, RES // 2:]
    assert (left == 0).sum() > (left == 1).sum()   # desert dominates the left
    assert (right == 1).sum() > (right == 0).sum()  # forest dominates the right


# ---------------------------------------------------------------------------
# End-to-end map generation (integration)
# ---------------------------------------------------------------------------

def test_every_region_is_a_single_connected_component():
    terrain = _split_terrain()
    gen = WorldMapGenerator(seed=7)
    wm = gen.generate(_compiled(_regions()), total_nodes=60,
                      map_width=1000.0, map_height=1000.0, terrain=terrain)
    by_region = {}
    for n in wm.nodes:
        by_region.setdefault(n.region, []).append(n.id)
    assert set(by_region) >= {"Desert Waste", "Greenwood"}
    for region, ids in by_region.items():
        assert region, "every node should have a region"
        comps = _connected_components(ids, wm.edges)
        assert comps == 1, f"region {region!r} fragmented into {comps} components"


def test_regions_are_spatially_separated_by_terrain():
    terrain = _split_terrain()
    gen = WorldMapGenerator(seed=7)
    wm = gen.generate(_compiled(_regions()), total_nodes=60,
                      map_width=1000.0, map_height=1000.0, terrain=terrain)
    desert_x = [n.x for n in wm.nodes if n.region == "Desert Waste"]
    forest_x = [n.x for n in wm.nodes if n.region == "Greenwood"]
    assert desert_x and forest_x
    # Desert nodes sit left (small x), forest nodes right (large x).
    assert np.mean(desert_x) < np.mean(forest_x)


def test_authored_locations_stay_in_their_territory():
    terrain = _split_terrain()
    gen = WorldMapGenerator(seed=7)
    wm = gen.generate(_compiled(_regions()), total_nodes=60,
                      map_width=1000.0, map_height=1000.0, terrain=terrain)
    named = {n.name: n for n in wm.nodes if n.name}
    # Authored locations keep their region (placed inside its territory), and the
    # desert's anchors sit left of the forest's anchor (the territory boundary is
    # the terrain-biased flood front, not exactly the geometric midline).
    for name in ("Dusthold", "Sunspire"):
        assert named[name].region == "Desert Waste"
    assert named["Elmgate"].region == "Greenwood"
    assert max(named["Dusthold"].x, named["Sunspire"].x) < named["Elmgate"].x


def test_no_keyword_region_gets_contiguous_territory_end_to_end():
    terrain = _split_terrain()
    regions = [
        {"name": "Void", "terrain": "mysterious void", "climate": "unknowable",
         "named_locations": [
             {"name": "Nowhere", "category": "settlement", "description": "?"}]},
        {"name": "Greenwood", "terrain": "dense temperate forest",
         "climate": "cool", "named_locations": [
             {"name": "Elmgate", "category": "settlement", "description": "town"}]},
    ]
    gen = WorldMapGenerator(seed=3)
    wm = gen.generate(_compiled(regions), total_nodes=60,
                      map_width=1000.0, map_height=1000.0, terrain=terrain)
    void_ids = [n.id for n in wm.nodes if n.region == "Void"]
    assert void_ids, "no-keyword region should still own nodes"
    assert _connected_components(void_ids, wm.edges) == 1
