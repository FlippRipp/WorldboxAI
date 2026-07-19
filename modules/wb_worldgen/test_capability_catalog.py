"""Tests for the combined capability catalog (B2 of the worldgen
architecture plan): the three registries' uniform describe() slices, the
aggregated catalog, and its markdown rendering — the document Arc C's
planner reads.

Run by explicit path (the root pytest.ini python_files whitelist does not
include module tests): python -m pytest modules/wb_worldgen/test_capability_catalog.py
"""

from wbworldgen.worldgen.catalog import capability_catalog, render_catalog_markdown


def test_catalog_covers_all_three_registries():
    cat = capability_catalog()

    step_ids = [e["id"] for e in cat["steps"]]
    # The built-in pipeline registers itself via the catalog's lazy import.
    for expected in ("world_form", "world_rules", "lore", "hierarchy_design",
                     "map_generation", "node_labeling", "node_descriptions"):
        assert expected in step_ids

    gen = {e["id"]: e for e in cat["generators"]}
    assert gen["world_map"]["implemented"] is True
    assert gen["star_system"]["implemented"] is False  # reserved id, still cataloged
    assert gen["interior"]["needs_llm_content"] is True

    passes = {e["id"]: e for e in cat["passes"]}
    assert set(passes) == {"label", "describe", "review"}
    assert passes["label"]["batchable"] is True
    assert passes["describe"]["after"] == ["label"]
    assert passes["review"]["unit"] == "map"
    assert passes["review"]["triggers"] == {"on_map_complete": "label"}


def test_every_entry_is_self_describing():
    # P1: a capability IS its catalog entry — kind, id, label and a
    # description the planner can select on, for every entry of every kind.
    cat = capability_catalog()
    kinds = {"steps": "step", "generators": "generator", "passes": "pass"}
    for key, kind in kinds.items():
        assert cat[key], f"no entries for {key}"
        for entry in cat[key]:
            assert entry["kind"] == kind
            assert entry["id"] and isinstance(entry["id"], str)
            assert entry["label"] and isinstance(entry["label"], str)
            assert entry["description"] and isinstance(entry["description"], str)


def test_markdown_render_lists_every_capability():
    cat = capability_catalog()
    doc = render_catalog_markdown(cat)

    assert "# Build capabilities" in doc
    for section in ("## Steps", "## Map generators", "## Enrichment passes"):
        assert section in doc
    for key in ("steps", "generators", "passes"):
        for entry in cat[key]:
            assert f"**{entry['id']}**" in doc

    # Contract annotations survive the rendering.
    assert "reserved, not implemented" in doc          # star_system / region
    assert "auto-runs when label completes a map" in doc  # review's trigger
    assert "per map" in doc and "per node" in doc
    assert "after label" in doc                        # describe's ordering


def test_enrich_passes_route_serves_the_catalog_slice():
    import asyncio

    import routes as world_routes
    from wbworldgen.worldgen.enrichment.registry import describe_passes

    payload = asyncio.run(world_routes.enrich_passes("any_world"))
    assert payload["passes"] == describe_passes()
