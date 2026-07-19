"""On-demand child-map expansion: a location opens into its own map.

One full-attention LLM call decides the child map's level and authors its
identity (label, description, entrance). The content contract then branches
on the chosen level's generator:

- authored generators (``interior``): the same call authors the locations
  with adjacency and connections back out; the generator lays them out
  deterministically.
- procedural generators (``world_map``, ``city_roadnet``): no locations are
  authored — the generator builds the map offline with a deterministic seed,
  and the play-time enrichment engine names/describes its nodes lazily,
  closest-to-the-story first, exactly like the root map.

Either way the result is a real MapRecord + ConnectionRecords — rendered,
traveled and fogged like any map. Generated lazily the first time the story
approaches the location (or on explicit request), cached in the world's
``maps/`` directory (write-once content; enrichment fills in names later).

Generator contract: every expansion MUST anchor the child to its parent with
at least one entrance connection — a child map you cannot enter or leave is
a hard error, never silently accepted.
"""

import hashlib
import logging
import re

from wbworldgen.mapmodel import grow_position
from wbworldgen.worldgen.generation.llm import json_retry_completion
from wbworldgen.worldgen.generation.abstract_graph import (
    MAX_PLANE_NODES, MAX_ROOT_NODES, ensure_crossing_nodes,
    layout_abstract_graph, mock_abstract_parsed, normalize_abstract_graph)
from wbworldgen.worldgen.enrichment.context import build_enrichment_context, collect_nodes_by_layer

logger = logging.getLogger(__name__)

MAX_DEPTH = 6

#: Node budget for procedurally generated child maps (a planet or city opened
#: from a larger map). Deliberately smaller than a root map.
CHILD_MAP_TOTAL_NODES = 60


def child_map_id(parent_map_id: str, node_id: str) -> str:
    """Deterministic child map id for an anchor position."""
    digest = hashlib.sha1(f"{parent_map_id}/{node_id}".encode("utf-8")).hexdigest()
    return f"m_{digest[:8]}"


def _steering_note_block(user_note: str) -> str:
    """The regeneration steering note as a prompt block (empty note → empty
    block). This is the D1 steering channel for authored map generation:
    without it, re-running the step feeds the model a byte-identical prompt
    and the result is an uncontrollable re-roll."""
    note = str(user_note or "").strip()
    if not note:
        return ""
    return ("\nSteering note for THIS generation — it overrides the "
            f"defaults above where they conflict:\n{note}\n")


def _normalize_locations(raw_locations, map_id: str, max_locations: int) -> list:
    """Authored locations clamped to the layout contract: ids are assigned
    server-side (never trust LLM ids), names dedup, exactly one entrance."""
    locations = []
    seen = set()
    entrance_seen = False
    for raw in (raw_locations or [])[:max_locations]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        is_entrance = bool(raw.get("is_entrance")) and not entrance_seen
        entrance_seen = entrance_seen or is_entrance
        locations.append({
            "id": f"{map_id}:n{len(locations) + 1}",
            "name": name,
            "type": str(raw.get("type", "room")).strip() or "room",
            "description": str(raw.get("description", "")).strip(),
            "additional_details": str(raw.get("additional_details", "")).strip(),
            "adjacent": raw.get("adjacent") if isinstance(raw.get("adjacent"), list) else [],
            "is_entrance": is_entrance,
        })
    if locations and not entrance_seen:
        locations[0]["is_entrance"] = True
    return locations


def _generator_spec(level: dict):
    from wbworldgen.worldgen.generation.registry import GENERATOR_REGISTRY
    return GENERATOR_REGISTRY.get(level.get("generator_id", "interior"))


def _is_authored(level: dict) -> bool:
    spec = _generator_spec(level)
    return bool(spec and spec.needs_llm_content)


def allowed_child_levels(compiled: dict, parent_map: dict) -> list[dict]:
    """Levels a child of this map may use: any level strictly below the
    parent's, plus the parent's own level when it declares ``nestable`` (a
    ship inside a station, a vault inside a castle). Only levels whose
    generator is implemented are offered."""
    levels = (compiled.get("hierarchy") or {}).get("levels") or []
    parent_type = (parent_map or {}).get("level_type", "")
    parent_idx = next((i for i, l in enumerate(levels)
                       if l.get("level_type") == parent_type), 0)
    allowed = []
    for i, level in enumerate(levels):
        if i > parent_idx or (i == parent_idx and level.get("nestable")):
            allowed.append(level)
    return [l for l in allowed
            if (spec := _generator_spec(l)) is not None and spec.build is not None]


def is_expandable(compiled: dict, map_id: str, node: dict) -> bool:
    """Whether a node can open into a child map: it is NAMED (the AI/player
    decide what deserves depth — no importance gate), a child level exists,
    depth allows it, and no child map is anchored there yet."""
    if not node or not node.get("name"):
        return False
    from wbworldgen.worldgen import mapspace as _ms
    parent = _ms.get_map(compiled, map_id)
    if parent is None:
        return False
    if len(_ms.breadcrumb(compiled, map_id)) >= MAX_DEPTH:
        return False
    if not allowed_child_levels(compiled, parent):
        return False
    return not _ms.children_by_anchor(compiled).get((map_id, node.get("id")))


def map_world_entries(map_record: dict, connections: list = None,
                      maps_by_id: dict = None) -> list[dict]:
    """RAG world-index entries for one child map + its connections. Must stay
    in the same format ``memory._build_world_entries`` emits for non-root v2
    maps so incremental embedding and a later full re-embed agree."""
    entries = []
    label = map_record.get("label", map_record.get("map_id", ""))
    if label or map_record.get("description"):
        entries.append({
            "text": f"Map: {label} ({map_record.get('level_type', 'map')}). "
                    f"{map_record.get('description', '')}".strip(),
            "source_type": "map",
            "source_id": map_record.get("map_id", ""),
            "region": label,
        })
    for node in map_record.get("nodes", []):
        if not node.get("name") or not node.get("description"):
            continue
        text = f"Location [{label}]: {node['name']} ({node.get('type', 'location')}). {node['description']}"
        if node.get("additional_details"):
            text += f" Storyteller notes: {node['additional_details']}"
        entries.append({
            "text": text,
            "source_type": "node",
            "source_id": node.get("id", ""),
            "region": label,
        })
    for c in connections or []:
        if c.get("hidden"):
            continue
        by_id = maps_by_id or {}
        from_label = (by_id.get((c.get("from") or {}).get("map_id")) or {}).get("label") \
            or (c.get("from") or {}).get("map_id", "")
        to_label = (by_id.get((c.get("to") or {}).get("map_id")) or {}).get("label") \
            or (c.get("to") or {}).get("map_id", "")
        entries.append({
            "text": f"Connection: {c.get('kind', 'passage')} '{c.get('name', '')}' linking "
                    f"{from_label} and {to_label}. {c.get('description', '')}".strip(),
            "source_type": "connection",
            "source_id": c.get("id", ""),
            "region": from_label,
        })
    return entries


class MapExpansionEngine:
    """One-call child-map expansion. Shares the ``GenServices`` LLM service,
    prompt library, temperature and semaphore/backoff, exactly like the
    (deprecated) SiteExpansionEngine it replaces."""

    def __init__(self, services):
        self._services = services

    @property
    def _llm(self):
        return self._services.llm

    async def expand(self, compiled: dict, parent_map_id: str, node: dict, *,
                     max_locations: int = 10, template_vocab: dict = None,
                     level_type: str = None, total_nodes: int = None,
                     world_id: str = None, must_include: str = None) -> dict:
        """Generate {"map": MapRecord, "connections": [ConnectionRecord]} for
        one anchor node. Raises on LLM failure or a violated entrance
        contract — nothing is persisted here.

        ``level_type`` pins the child's level (a pregenerate plan or an
        explicit caller choice); otherwise the LLM picks from the allowed
        levels. ``total_nodes`` sizes procedurally generated children.
        ``world_id`` enables terrain rasters for terrain-flagged levels (they
        persist under the world's terrain directory). ``must_include`` is a
        place the story already went to inside this location — the authored
        interior is told to include it."""
        from wbworldgen.worldgen import mapspace as _ms
        max_locations = max(4, min(int(max_locations or 10), 16))
        parent_map = _ms.get_map(compiled, parent_map_id) or {}
        levels = allowed_child_levels(compiled, parent_map)
        if not levels:
            raise ValueError(f"No child levels available below map {parent_map_id}")
        pinned = next((l for l in levels if level_type
                       and l.get("level_type") == str(level_type).strip()), None)
        if pinned is not None:
            levels = [pinned]

        # Places established as being inside this node (authored with
        # relation "inside", or visited by the story) MUST appear on the
        # child map.
        must_list = [
            {"name": str(c.get("name", "")).strip(),
             "description": str(c.get("description", "")).strip()}
            for c in (node.get("contained_locations") or [])
            if isinstance(c, dict) and str(c.get("name", "")).strip()
        ]
        mi = str(must_include or "").strip()
        if mi and mi.lower() not in {c["name"].lower() for c in must_list}:
            must_list.append({"name": mi, "description": ""})

        if not self._llm or self._llm.mode == "mock":
            parsed = self._mock_content(node, levels, max_locations, must_list)
        else:
            all_nodes, _ = collect_nodes_by_layer(compiled)
            context = build_enrichment_context(node, all_nodes, compiled, include_descriptions=True)
            # Ideation notes whose subject names this node (C5/N3): the
            # child map about to be authored IS that subject, so its
            # generation call gets them in full.
            from wbworldgen.worldgen.notes import notes_matching_name
            context["child_notes"] = notes_matching_name(compiled, node.get("name", ""))
            parsed = await self._live_expand(node, context, parent_map, levels,
                                             max_locations, template_vocab,
                                             must_include=must_list)

        chosen = next((l for l in levels
                       if l.get("level_type") == str(parsed.get("level_type", "")).strip()),
                      levels[0])
        if _is_authored(chosen):
            # Contract, not a hope: any established place the LLM left out is
            # merged in, adjoining the entrance.
            locs = parsed.get("locations") if isinstance(parsed.get("locations"), list) else []
            have = {str(l.get("name", "")).strip().lower()
                    for l in locs if isinstance(l, dict)}
            missing = [m for m in must_list if m["name"].lower() not in have]
            if missing:
                entrance = next((str(l.get("name", "")).strip() for l in locs
                                 if isinstance(l, dict) and l.get("is_entrance")), "")
                for m in missing:
                    locs.append({"name": m["name"], "type": "room",
                                 "description": m["description"],
                                 "adjacent": [entrance] if entrance else [],
                                 "is_entrance": False})
                parsed["locations"] = locs
                max_locations = max(max_locations, len(locs))
            return self._build_map(compiled, parent_map_id, node, parsed, chosen, max_locations)
        # Procedural builds are CPU-bound (terrain rasters take seconds) —
        # keep the event loop free.
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._build_procedural_map(
                compiled, parent_map_id, node, parsed, chosen, total_nodes, world_id))

    async def grow(self, compiled: dict, map_record: dict, description: str,
                   near_node_id: str = None, template_vocab: dict = None) -> dict | None:
        """Author ONE new location onto an existing child map — the play-time
        "the story went somewhere inside this place that isn't on its map yet"
        path (an NPC leads the player to the school's storage building).

        The new node adjoins the locations the LLM says it does (falling back
        to the player's current node) and is positioned right beside them, so
        places that belong together stay together by construction. A request
        matching an existing location returns that location (with the edges
        it already has) instead of creating a duplicate.

        Returns ``{"node", "edges", "created"}`` — the map record is mutated
        in place when ``created`` — or ``{"belongs_outside": True}`` when the
        LLM vetoes the request as not an interior place at all (its own
        destination in the wider world; the caller falls through to overworld
        authoring), or None on unusable output. Raises on LLM failure
        (callers decide the fallback).
        """
        description = str(description or "").strip()
        if not description:
            return None
        nodes = map_record.setdefault("nodes", [])
        named = [n for n in nodes if n.get("name")]
        near_node = next((n for n in nodes if n.get("id") == near_node_id), None)

        if not self._llm or self._llm.mode == "mock":
            cleaned = description.rstrip(".")
            parsed = {
                "name": cleaned[:1].upper() + cleaned[1:],
                "type": "place",
                "description": f"{cleaned}.",
                "adjacent": [near_node["name"]] if near_node and near_node.get("name") else [],
            }
        else:
            parsed = await self._live_grow(compiled, map_record, named, description,
                                           near_node, template_vocab)

        if parsed.get("belongs_outside"):
            return {"belongs_outside": True}

        def _match(hit: dict) -> dict:
            # An existing match carries the node's real edges so a caller with
            # a stale/partial copy of this map can wire the node in routably.
            hit_id = hit.get("id")
            edges = [dict(e) for e in map_record.get("edges") or []
                     if e.get("from") == hit_id or e.get("to") == hit_id]
            return {"node": hit, "edges": edges, "created": False}

        by_name = {str(n.get("name", "")).strip().lower(): n for n in named}
        existing_ref = str(parsed.get("existing", "")).strip().lower()
        if existing_ref and existing_ref in by_name:
            return _match(by_name[existing_ref])

        name = str(parsed.get("name", "")).strip()
        if not name:
            return None
        if name.lower() in by_name:
            # The LLM authored a place that already exists — treat as a match.
            return _match(by_name[name.lower()])

        # Anchors: the locations the new place adjoins. They both wire the
        # edges and position the node, so resolution failures fall back to
        # where the story is (the player's node), never to "anywhere".
        raw_adjacent = parsed.get("adjacent") if isinstance(parsed.get("adjacent"), list) else []
        anchors = []
        for ref in raw_adjacent[:3]:
            hit = by_name.get(str(ref).strip().lower())
            if hit is not None and hit not in anchors:
                anchors.append(hit)
        if not anchors and near_node is not None:
            anchors = [near_node]
        if not anchors and nodes:
            anchors = [max(nodes, key=lambda n: n.get("importance", 0) or 0)]

        map_id = map_record.get("map_id", "")
        k = len(nodes) + 1
        taken = {n.get("id") for n in nodes}
        while f"{map_id}:g{k}" in taken:
            k += 1
        node = {
            "id": f"{map_id}:g{k}",
            "name": name,
            "type": str(parsed.get("type", "place")).strip() or "place",
            "description": str(parsed.get("description", "")).strip(),
            "additional_details": str(parsed.get("additional_details", "")).strip(),
            "label_description": "",
            "importance": min(10, max(1, 3 + len(anchors))),
        }
        node["x"], node["y"] = grow_position(map_record, anchors)

        edges = []
        for anchor in anchors:
            dist = ((node["x"] - anchor.get("x", 0.0)) ** 2
                    + (node["y"] - anchor.get("y", 0.0)) ** 2) ** 0.5
            edges.append({"from": anchor.get("id"), "to": node["id"],
                          "distance": round(max(dist, 1.0), 2)})
        nodes.append(node)
        map_record.setdefault("edges", []).extend(edges)
        return {"node": node, "edges": edges, "created": True}

    async def _live_grow(self, compiled: dict, map_record: dict, named_nodes: list,
                         description: str, near_node: dict = None,
                         template_vocab: dict = None) -> dict:
        services = self._services
        lore = compiled.get("lore", {}) or {}
        rules = compiled.get("rules", {}) or {}
        label = map_record.get("label", map_record.get("map_id", ""))

        locations_block = "\n".join(
            f"- {n['name']} ({n.get('type', 'place')}): "
            f"{n.get('description') or n.get('label_description', '')}"
            for n in named_nodes) or "- (none yet)"
        near_line = (f"The player is currently at: {near_node['name']}.\n"
                     if near_node and near_node.get("name") else "")

        system = services.prompts(
            "map_grow_system",
            "You are a world-building AI adding ONE new location to an existing map "
            "because the story needs it. Keep it consistent with the place it is part "
            "of and the locations already there. Output ONLY valid JSON.",
        )
        user_msg = services.prompts(
            "map_grow_user",
            f"""World: {lore.get('world_name', 'Unknown')} ({rules.get('genre', '')}, {rules.get('tone', '')})

Map: {label} ({map_record.get('level_type', 'interior')}) — {map_record.get('description', '')}

Existing locations on this map:
{locations_block}

{near_line}The story needs this place: "{description}"

If one of the existing locations above already IS this place, return {{"existing": "<its exact name>"}} and nothing else.
If this place does NOT belong inside {label} at all — you could not walk there without leaving
{label}; it is its own destination out in the wider world that merely happens to be close —
return {{"belongs_outside": true}} and nothing else, and it will be placed outside instead.
Otherwise author it: a unique name, a short type, a 1-2 sentence description (the surface — what a
visitor perceives), additional_details (1-2 sentences for the storyteller only: depth, a hook, hidden
facts marked with a leading 'Secret:'), and "adjacent" — 1-3 existing location names it directly
adjoins. Unless the request implies otherwise, it should adjoin the player's current location or
somewhere right next to it.

Output ONLY valid JSON:
{{"name": "...", "type": "...", "description": "...", "additional_details": "...", "adjacent": ["..."]}}""",
            map_label=label,
            map_description=map_record.get("description", ""),
            world_name=lore.get("world_name", "Unknown"),
            request=description,
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        await services.backoff.wait()
        async with services.semaphore:
            try:
                return await json_retry_completion(
                    self._llm,
                    messages=messages,
                    model=self._llm.reader_model,
                    temperature=services.temperature or 0.9,
                    inspector_ctx={"call_type": "world_build", "step": "map:grow"},
                    step_label=f"map:grow:{map_record.get('map_id', '')}",
                    retry_attempts=services.json_retry_attempts,
                )
            except Exception as e:
                services.backoff.note_rate_limit(e)
                logger.error("Map grow failed for %s: %s", map_record.get("map_id"), e)
                raise

    # --- authored root map (root-as-first-expansion) ------------------------

    def mock_root_map(self, world_state: dict, level: dict,
                      max_locations: int = 10) -> dict:
        """Deterministic offline authored root — the sync core the mock/seed
        paths share."""
        from wbworldgen.worldgen.compiler import build_compiled_for_map
        compiled = build_compiled_for_map(world_state)
        name = (compiled.get("lore", {}) or {}).get("world_name") or "The World"
        parsed = self._mock_content({"name": name, "type": level.get("level_type", "")},
                                    [level], min(6, max_locations))
        return self._layout_root(parsed, level, max_locations)

    async def expand_root(self, world_state: dict, user_prompt: str, level: dict,
                          max_locations: int = 12, force_mock: bool = False,
                          user_note: str = "") -> dict:
        """Author the ROOT map for a world whose root level uses an authored
        (needs_llm_content) generator — the whole playable world is one
        interior-style place (a mansion, a generation ship, a single keep).
        One full-attention call authors the locations; the level's generator
        lays them out. Returns a map_generation step-data dict
        ({nodes, edges, config, generator_id}) — no parent, no entrance
        connection, exactly like the procedural root path's output.
        ``user_note`` is the regeneration steering channel (D1) rendered
        into the authoring prompt — without it a re-run is an unsteerable
        re-roll of the identical prompt."""
        if force_mock or not self._llm or self._llm.mode == "mock":
            return self.mock_root_map(world_state, level, max_locations)
        from wbworldgen.worldgen.compiler import build_compiled_for_map
        compiled = build_compiled_for_map(world_state)
        parsed = await self._live_expand_root(compiled, user_prompt, level,
                                              max_locations, user_note)
        return self._layout_root(parsed, level, max_locations)

    def _layout_root(self, parsed: dict, level: dict, max_locations: int) -> dict:
        from wbworldgen.worldgen.generation.registry import get_generator
        locations = _normalize_locations(parsed.get("locations"), "root", max_locations)
        if not locations:
            raise ValueError("Authored root map produced no valid locations")
        generated = get_generator(level.get("generator_id", "interior")).build(
            {"map_id": "root", "locations": locations})
        generated.pop("entrance_node_id", None)
        return {
            "nodes": generated["nodes"],
            "edges": generated["edges"],
            "config": generated["config"],
            "generator_id": level.get("generator_id", "interior"),
            "description": str(parsed.get("description", "")).strip(),
        }

    async def _live_expand_root(self, compiled: dict, user_prompt: str,
                                level: dict, max_locations: int,
                                user_note: str = "") -> dict:
        services = self._services
        lore = compiled.get("lore", {}) or {}
        name = lore.get("world_name") or "the world"
        note_block = _steering_note_block(user_note)
        system = services.prompts(
            "map_root_system",
            "You are a world-building AI designing the single playable map an entire "
            "interactive story takes place on: one contained place — a building, complex, "
            "vessel or compound — whose rooms and areas ARE the whole world. Ground "
            "everything in the provided world context. Output ONLY valid JSON.",
        )
        user_msg = services.prompts(
            "map_root_user",
            f"""World: {name} ({lore.get('genre', '')}, {lore.get('tone', '')})
World premise: {user_prompt}

This world's whole playable space is ONE map at the "{level.get('level_type', 'interior')}" level:
{level.get('guidance', level.get('label', ''))}
{note_block}
Design 8-{max_locations} distinct locations (rooms, halls, decks, courts...). Exactly ONE
location must have "is_entrance": true — the main way in from the outside world. Each
location gets a name, a short type, a 1-2 sentence description (the surface — what a
visitor perceives), additional_details (1-2 sentences for the storyteller only: depth, a
hook, hidden facts marked with a leading 'Secret:'), and which other locations it directly
adjoins (by name). Make the geography coherent: wings, floors and passages that read like
one real place.

Output ONLY valid JSON:
{{"label": "...", "description": "2-3 sentences on how this place is laid out",
"locations": [{{"name": "...", "type": "...", "description": "...", "additional_details": "...", "adjacent": ["..."], "is_entrance": false}}, ...]}}""",
            world_name=name,
            world_premise=user_prompt,
            max_locations=str(max_locations),
            user_note=user_note,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        return await json_retry_completion(
            self._llm,
            messages=messages,
            model=self._llm.reader_model,
            temperature=services.temperature or 0.9,
            inspector_ctx={"call_type": "world_build", "step": "map:root"},
            step_label="map:root",
            retry_attempts=services.json_retry_attempts,
        )

    # --- authored abstract root (map_style "abstract") ----------------------

    def _abstract_inputs(self, world_state: dict) -> tuple:
        """(seed_prompt, lore, rules, areas, scopes, parallel_maps) for the
        authored abstract root flow."""
        from wbworldgen.worldgen import design as _design
        from wbworldgen.worldgen.compiler import collect_scope_content
        steps_data = world_state.get("steps", {})
        landmarks_data = steps_data.get("natural_landmarks", {}).get("data", {}) or {}
        areas = [a for a in landmarks_data.get("areas", []) or []
                 if isinstance(a, dict) and str(a.get("name", "")).strip()]
        scopes = collect_scope_content(steps_data)
        parallel = _design.parallel_maps(world_state)
        lore = steps_data.get("lore", {}).get("data", {}) or {}
        rules = steps_data.get("world_rules", {}).get("data", {}) or {}
        return world_state.get("seed_prompt", ""), lore, rules, areas, scopes, parallel

    def mock_abstract_root(self, world_state: dict, level: dict) -> dict:
        """Deterministic offline authored-abstract root — same assembly as the
        live path, with the per-layer LLM call replaced by the mock author."""
        seed_prompt, _lore, _rules, areas, scopes, parallel = \
            self._abstract_inputs(world_state)
        root_prefix = "root_" if parallel else ""
        parsed_root = mock_abstract_parsed(
            "the world", areas, scopes.get("", {}).get("named_locations", []))
        parsed_planes = [
            mock_abstract_parsed(pm["label"], [],
                                 scopes.get(pm["label"], {}).get("named_locations", []),
                                 MAX_PLANE_NODES)
            for pm in parallel]
        return self._assemble_abstract_root(
            seed_prompt, areas, scopes, parallel, level,
            parsed_root, parsed_planes, root_prefix)

    async def expand_abstract_root(self, world_state: dict, user_prompt: str,
                                   level: dict, directive: str = "",
                                   world_kind: str = "",
                                   force_mock: bool = False,
                                   user_note: str = "") -> dict:
        """Author the ROOT map for an abstract world (map_style "abstract"):
        a solar system, a dream web — a graph of real conceptual places, not
        a procedural scatter. One full-attention call authors the root layer
        (reading the hierarchy guidance, the world-design map directive, the
        authored areas and every named location); each parallel plane gets
        its own call; crossings are paired and the deterministic layout
        places everything. Returns map_generation step data in the same
        shape as the procedural path (flat map, or legacy multilayer with
        parallel planes). ``user_note`` is the regeneration steering
        channel (D1), rendered into every layer's authoring prompt —
        without it a re-run is an unsteerable re-roll of the identical
        prompt (the Ecstasy Veil live run lost a finished root to exactly
        that)."""
        if force_mock or not self._llm or self._llm.mode == "mock":
            return self.mock_abstract_root(world_state, level)
        seed_prompt, lore, rules, areas, scopes, parallel = \
            self._abstract_inputs(world_state)
        root_prefix = "root_" if parallel else ""

        crossing_specs = []
        for pm in parallel:
            try:
                count = max(1, min(6, int(pm.get("connection_count") or 2)))
            except (TypeError, ValueError):
                count = 2
            crossing_specs.append({
                "label": pm["label"],
                "kind": pm.get("connection_kind") or "passage",
                "count": count,
                "description": pm.get("description", ""),
            })

        # Ideation notes whose subject names a layer being authored right
        # now (C5/N3): there is no map record to bind to yet, so match the
        # layer's label directly.
        from wbworldgen.worldgen.notes import notes_matching_name

        def _layer_notes(name: str) -> str:
            matched = notes_matching_name(world_state, name)
            if not matched:
                return ""
            return ("\nThe world's creator agreed on design notes about this "
                    "map — established facts it must embody:\n"
                    + "\n".join(f"- {n}" for n in matched))

        parsed_root = await self._live_abstract_layer(
            lore, rules, user_prompt, world_kind=world_kind,
            label=lore.get("world_name", "") or "the world",
            level_type=level.get("level_type", "world"),
            guidance=level.get("guidance", "")
            + _layer_notes(lore.get("world_name", "")),
            directive=directive,
            areas=areas,
            named_locations=scopes.get("", {}).get("named_locations", []),
            crossing_specs=crossing_specs, max_nodes=MAX_ROOT_NODES,
            plane_description="", step_label="map:abstract_root",
            user_note=user_note)
        parsed_planes = []
        for pm in parallel:
            spec = next(s for s in crossing_specs if s["label"] == pm["label"])
            parsed_planes.append(await self._live_abstract_layer(
                lore, rules, user_prompt, world_kind=world_kind,
                label=pm["label"], level_type=pm.get("level_type", "world"),
                guidance=_layer_notes(pm["label"]), directive="", areas=[],
                named_locations=scopes.get(pm["label"], {}).get("named_locations", []),
                crossing_specs=[{**spec, "label": "main",
                                 "description": "the main world map"}],
                max_nodes=MAX_PLANE_NODES,
                plane_description=pm.get("description", ""),
                step_label=f"map:abstract_plane:{pm['label']}",
                user_note=user_note))

        return self._assemble_abstract_root(
            seed_prompt, areas, scopes, parallel, level,
            parsed_root, parsed_planes, root_prefix)

    def _assemble_abstract_root(self, seed_prompt: str, areas: list,
                                scopes: dict, parallel: list, level: dict,
                                parsed_root: dict, parsed_planes: list,
                                root_prefix: str) -> dict:
        """Normalize + lay out authored layers, pair plane crossings, and
        shape the result exactly like the procedural path's output. A layer
        whose authored output normalizes to nothing degrades to the mock
        author for that layer — an abstract world never falls back to the
        Poisson scatter."""
        root_locs = scopes.get("", {}).get("named_locations", [])
        root_graph = normalize_abstract_graph(
            parsed_root, root_locs, areas, root_prefix, MAX_ROOT_NODES)
        if not root_graph["nodes"]:
            root_graph = normalize_abstract_graph(
                mock_abstract_parsed("the world", areas, root_locs),
                root_locs, areas, root_prefix, MAX_ROOT_NODES)

        if not parallel:
            result = layout_abstract_graph(root_graph, areas,
                                           generated_from=seed_prompt)
            for node in result["nodes"]:
                node.pop("crossing", None)
            return result

        plane_graphs = []
        for i, pm in enumerate(parallel):
            lid = re.sub(r"[^a-z0-9]+", "_", str(pm["label"]).lower()).strip("_") \
                or f"parallel_{i + 1}"
            plane_locs = scopes.get(pm["label"], {}).get("named_locations", [])
            parsed = parsed_planes[i] if i < len(parsed_planes) else {}
            graph = normalize_abstract_graph(
                parsed, plane_locs, [], f"{lid}_", MAX_PLANE_NODES)
            if not graph["nodes"]:
                graph = normalize_abstract_graph(
                    mock_abstract_parsed(pm["label"], [], plane_locs,
                                         MAX_PLANE_NODES),
                    plane_locs, [], f"{lid}_", MAX_PLANE_NODES)
            plane_graphs.append((lid, pm, graph))

        connections = []
        counter = 0
        for lid, pm, graph in plane_graphs:
            try:
                count = max(1, min(6, int(pm.get("connection_count") or 2)))
            except (TypeError, ValueError):
                count = 2
            kind = pm.get("connection_kind") or "passage"
            root_cross = ensure_crossing_nodes(
                root_graph, pm["label"], kind, count, root_prefix)
            plane_cross = ensure_crossing_nodes(
                graph, "main", kind, count, f"{lid}_")
            for fn, tn in zip(root_cross, plane_cross):
                lc_id = f"lc_{counter:04d}"
                fn["interlayer_connection_id"] = lc_id
                tn["interlayer_connection_id"] = lc_id
                connections.append({
                    "id": lc_id,
                    "from_layer_id": "root", "from_node_id": fn["id"],
                    "to_layer_id": lid, "to_node_id": tn["id"],
                    "connection_type": kind,
                    "name": f"{kind.replace('_', ' ').title()} #{counter + 1}",
                    "description": pm.get("description", ""),
                    "bidirectional": True,
                })
                counter += 1

        layers = []
        root_map = layout_abstract_graph(root_graph, areas,
                                         generated_from=seed_prompt)
        root_description = root_map.pop("description", "")
        root_map["layer_id"] = "root"
        layers.append({
            "layer_id": "root", "name": "",
            "description": root_description,
            "layer_type": level.get("level_type") or "world",
            "index": 0, "map": root_map,
        })
        for i, (lid, pm, graph) in enumerate(plane_graphs):
            plane_map = layout_abstract_graph(graph, [], generated_from=seed_prompt)
            plane_map.pop("description", None)
            plane_map["layer_id"] = lid
            layers.append({
                "layer_id": lid, "name": pm["label"],
                "description": pm.get("description", ""),
                "layer_type": pm.get("level_type") or "world",
                "index": i + 1, "map": plane_map,
            })
        for layer in layers:
            for node in layer["map"]["nodes"]:
                node.pop("crossing", None)

        return {
            "layers": layers,
            "connections": connections,
            "config": {
                "total_nodes": sum(len(l["map"]["nodes"]) for l in layers),
                "generated_from": seed_prompt,
            },
        }

    async def _live_abstract_layer(self, lore: dict, rules: dict,
                                   user_prompt: str, *, world_kind: str,
                                   label: str, level_type: str, guidance: str,
                                   directive: str, areas: list,
                                   named_locations: list, crossing_specs: list,
                                   max_nodes: int, plane_description: str,
                                   step_label: str, user_note: str = "") -> dict:
        services = self._services
        name = lore.get("world_name") or "the world"
        note_block = _steering_note_block(user_note)

        kind_line = f"World kind: {world_kind}\n" if world_kind else ""
        if plane_description:
            scale_block = (f'This map is a PARALLEL plane beside the main world map: '
                           f'"{label}" ({level_type}) — {plane_description}\n'
                           f'Nodes here have no "region" (leave it empty).')
        else:
            scale_block = (f'The root map of this world is ONE "{level_type}" map:\n'
                           f'{guidance or label}')
        directive_line = f"\nMap directive: {directive}" if directive else ""

        if areas:
            area_lines = "\n".join(
                f"- {a.get('name', '')}: {a.get('terrain', '')}"
                + (f" — {a.get('description', '')}" if a.get('description') else "")
                for a in areas)
            areas_block = ("\nAreas dividing this map (every node's \"region\" must be "
                           "one of these names, copied exactly, and every area must "
                           f"hold at least one node):\n{area_lines}\n")
        else:
            areas_block = ""

        if named_locations:
            loc_lines = []
            for loc in named_locations:
                bits = [loc.get("category", "place")]
                if loc.get("region"):
                    bits.append(f"area: {loc['region']}")
                if loc.get("part_of"):
                    bits.append(f"{loc.get('relation', 'adjacent')} {loc['part_of']}")
                head = f"- {loc.get('name', '')} ({', '.join(bits)})"
                desc = str(loc.get("description", "")).strip()
                loc_lines.append(f"{head}: {desc}" if desc else head)
            locations_block = (
                "\nAuthored places that must appear. Make each one either a node "
                "itself (if it IS a place at this map's scale) or an entry in a "
                "fitting node's \"contains\" (a venue-scale place lives INSIDE "
                "the larger place it belongs to — never as its sibling on this "
                "map):\n" + "\n".join(loc_lines) + "\n")
        else:
            locations_block = ""

        crossing_block = ""
        for spec in crossing_specs:
            crossing_block += (
                f"\nInclude exactly {spec['count']} node(s) of kind "
                f"\"{spec['kind']}\" — crossings to \"{spec['label']}\" "
                f"({spec['description']}). Mark each of them with "
                f"\"crossing\": \"{spec['label']}\".")

        system = services.prompts(
            "map_abstract_system",
            "You are a world-building AI designing a map as a graph of real places. "
            "This map is ABSTRACT: no procedural terrain, no filler — every node is "
            "a distinct, named, meaningful place at this map's scale (in a solar "
            "system: the star, planets, moons, stations; in a dream web: the great "
            "dreams), and every edge is a real travel route. Ground everything in "
            "the provided world context and authored places. Output ONLY valid JSON.",
        )
        user_msg = services.prompts(
            "map_abstract_user",
            f"""World: {name} ({rules.get('genre', '')}, {rules.get('tone', '')})
{kind_line}World premise: {user_prompt}

{scale_block}{directive_line}{note_block}
{areas_block}{locations_block}
Design {max(6, max_nodes // 2)}-{max_nodes} nodes — the real structure of this map at its own
scale. Each node: "name"; "kind" (this world's own noun for what it is: planet,
station, moon, gate, dream...); "region" (one of the areas above, or empty);
"importance" 1-10 (how central it is); "description" (1-2 vivid sentences — the
surface, what a visitor perceives); "additional_details" (1-2 sentences for the
storyteller only: depth, a hook, hidden facts marked with a leading 'Secret:');
"adjacent" (names of nodes it has direct travel routes to — every node must be
reachable); "contains" (names of authored places from the list above that live
inside/on this node).

Structural hints — use them ONLY when the premise implies that shape, else
omit them and nodes simply cluster by region: "center": true on the ONE node
everything arranges around (a system's star, a realm's citadel); "orbit"
(1 = innermost) on nodes that sit on concentric rings around the center —
planets' orbital rings, ringed wards; "parent" naming the node this one is a
satellite of (a moon of its planet, a station over a world) — it is placed
hugging that node.{crossing_block}

Output ONLY valid JSON:
{{"description": "2-3 sentences on this map's overall shape",
"nodes": [{{"name": "...", "kind": "...", "region": "...", "importance": 5,
"description": "...", "adjacent": ["..."], "contains": ["..."],
"center": false, "orbit": 0, "parent": "", "crossing": ""}}]}}""",
            world_name=name,
            world_premise=user_prompt,
            map_label=label,
            level_type=level_type,
            max_nodes=str(max_nodes),
            user_note=user_note,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        try:
            return await json_retry_completion(
                self._llm,
                messages=messages,
                model=self._llm.reader_model,
                temperature=services.temperature or 0.9,
                inspector_ctx={"call_type": "world_build", "step": "map:abstract"},
                step_label=step_label,
                retry_attempts=services.json_retry_attempts,
            )
        except Exception as e:
            logger.error("Abstract map authoring failed for %s: %s — layer "
                         "degrades to the offline author", label, e)
            return {}

    # --- result shaping -----------------------------------------------------

    def _build_map(self, compiled: dict, parent_map_id: str, node: dict,
                   parsed: dict, level: dict, max_locations: int) -> dict:
        node_id = node.get("id", "")
        map_id = child_map_id(parent_map_id, node_id)
        raw_locations = parsed.get("locations")
        if not isinstance(raw_locations, list) or not raw_locations:
            raise ValueError(f"Map expansion for {node_id} returned no locations")

        locations = _normalize_locations(raw_locations, map_id, max_locations)
        if not locations:
            raise ValueError(f"Map expansion for {node_id} produced no valid locations")

        from wbworldgen.worldgen.generation.registry import get_generator
        generated = get_generator(level.get("generator_id", "interior")).build(
            {"map_id": map_id, "locations": locations})
        entrance_node_id = generated.pop("entrance_node_id", None) \
            or (generated["nodes"][0]["id"] if generated["nodes"] else None)

        record = {
            "map_id": map_id,
            "label": str(parsed.get("label", "")).strip() or f"Inside {node.get('name', node_id)}",
            "level_type": level.get("level_type", "interior"),
            "description": str(parsed.get("description", "")).strip(),
            "parent_map_id": parent_map_id,
            "anchor_node_id": node_id,
            "generator_id": level.get("generator_id", "interior"),
            "nodes": generated["nodes"],
            "edges": generated["edges"],
            "config": generated["config"],
            "schema": 2,
        }

        # Connections: the mandatory entrance plus any extra authored ways
        # out, resolved against the parent map's node names.
        from wbworldgen.worldgen import mapspace as _ms
        parent_nodes_by_name = {
            str(n.get("name", "")).strip().lower(): n
            for n in _ms.map_nodes(compiled, parent_map_id) if n.get("name")}
        child_by_name = {str(n.get("name", "")).strip().lower(): n
                         for n in generated["nodes"]}
        connections = []

        def _connection(kind, name, description, child_node_id, parent_node_id,
                        travel, requirements, hidden=False):
            return {
                "id": f"c_{hashlib.sha1(f'{map_id}/{len(connections)}/{child_node_id}'.encode()).hexdigest()[:8]}",
                "from": {"map_id": parent_map_id, "node_id": parent_node_id},
                "to": {"map_id": map_id, "node_id": child_node_id},
                "kind": kind or "entrance",
                "name": name or "",
                "description": description or "",
                "travel": travel,
                "bidirectional": True,
                "requirements": requirements or "",
                "hidden": bool(hidden),
                "origin": "generated",
            }

        raw_connections = parsed.get("connections")
        if isinstance(raw_connections, list):
            for raw in raw_connections[:6]:
                if not isinstance(raw, dict):
                    continue
                child_ref = str(raw.get("at_location", "")).strip().lower()
                child_node = child_by_name.get(child_ref)
                if child_node is None and entrance_node_id:
                    child_node = next((n for n in generated["nodes"]
                                       if n["id"] == entrance_node_id), None)
                if child_node is None:
                    continue
                parent_ref = str(raw.get("to_parent_location", "")).strip().lower()
                parent_node = parent_nodes_by_name.get(parent_ref) or {"id": node_id}
                travel_raw = raw.get("travel", "instant")
                if isinstance(travel_raw, (int, float)) or (
                        isinstance(travel_raw, str) and travel_raw.strip().isdigit()):
                    travel = {"mode": "journey", "turns": max(1, int(travel_raw))}
                else:
                    travel = {"mode": "instant"}
                connections.append(_connection(
                    str(raw.get("kind", "")).strip(), str(raw.get("name", "")).strip(),
                    str(raw.get("description", "")).strip(), child_node["id"],
                    parent_node.get("id", node_id), travel,
                    str(raw.get("requirements", "")).strip(),
                    hidden=bool(raw.get("hidden"))))

        # Hidden ways don't count as an anchor — the player must always have
        # a visible way in (the entrance); secrets are extra, never the door.
        anchored = any(
            (c["from"]["node_id"] == node_id or c["to"]["node_id"] == node_id)
            and not c.get("hidden")
            for c in connections)
        if not anchored:
            if entrance_node_id is None:
                raise ValueError(f"Map expansion for {node_id} has no entrance node")
            connections.insert(0, _connection(
                str(parsed.get("entrance_kind", "")).strip() or "entrance",
                str(parsed.get("entrance_name", "")).strip(),
                str(parsed.get("entrance_description", "")).strip(),
                entrance_node_id, node_id, {"mode": "instant"}, ""))

        return {"map": record, "connections": connections}

    def _build_procedural_map(self, compiled: dict, parent_map_id: str, node: dict,
                              parsed: dict, level: dict, total_nodes: int = None,
                              world_id: str = None) -> dict:
        """Procedural child map (world_map/city_roadnet levels): the level's
        generator builds the geography offline with a deterministic seed; the
        play-time enrichment engine names and describes its nodes later,
        closest-to-the-story first. The LLM authored only the map's identity
        (label, description, entrance). Terrain-flagged world_map levels get
        their own raster stack first (a planet with real elevation/biomes),
        persisted per map id — the map screen and enrichment sampling pick it
        up by that key."""
        from wbworldgen.worldgen.generation.registry import get_generator
        node_id = node.get("id", "")
        map_id = child_map_id(parent_map_id, node_id)
        # Child-scoped context: the world premise rides along for flavor; the
        # parent's regions do not (a planet does not inherit the overworld's
        # geography).
        scoped = {
            "generated_from": compiled.get("generated_from", ""),
            "lore": compiled.get("lore", {}) or {},
            "regions": {"regions": []},
        }
        seed = int(hashlib.sha1(map_id.encode("utf-8")).hexdigest()[:8], 16) or 1
        terrain_layers, terrain_meta = None, None
        if level.get("terrain") and level.get("generator_id", "world_map") == "world_map":
            terrain_layers, terrain_meta = self._build_child_terrain(
                map_id, seed, parsed.get("label") or node.get("name") or map_id, world_id)
        generated = get_generator(level.get("generator_id", "world_map")).build({
            "compiled_world": scoped,
            "total_nodes": max(20, min(int(total_nodes or CHILD_MAP_TOTAL_NODES), 200)),
            "seed": seed,
            "id_prefix": f"{map_id}:",
            "terrain": terrain_layers,
        })
        if not generated.get("nodes"):
            raise ValueError(f"Procedural expansion for {node_id} produced no nodes")
        if terrain_meta:
            generated.setdefault("config", {})["terrain"] = terrain_meta

        record = {
            "map_id": map_id,
            "label": str(parsed.get("label", "")).strip()
                     or (node.get("name") or f"Inside {node_id}"),
            "level_type": level.get("level_type", ""),
            "description": str(parsed.get("description", "")).strip(),
            "parent_map_id": parent_map_id,
            "anchor_node_id": node_id,
            "generator_id": level.get("generator_id", "world_map"),
            "nodes": generated["nodes"],
            "edges": generated.get("edges", []),
            "config": generated.get("config", {}),
            "schema": 2,
        }
        # Optional geometry extras — carried by reference when present.
        for key in ("regions", "roads"):
            if generated.get(key):
                record[key] = generated[key]

        # Places established as inside this node become named locations of the
        # procedural child (a capital the lore already promised the planet).
        contained = [
            {"name": str(c.get("name", "")).strip(), "category": "landmark",
             "description": str(c.get("description", "")).strip()}
            for c in (node.get("contained_locations") or [])
            if isinstance(c, dict) and str(c.get("name", "")).strip()
        ]
        if contained:
            from wbworldgen.worldgen.generation.binding import bind_named_locations
            bind_named_locations(record["nodes"], contained, record["edges"])

        # Arrival point: the map's most important node — its natural hub.
        arrival = max(record["nodes"], key=lambda n: n.get("importance", 0) or 0)
        digest = hashlib.sha1(f"{map_id}/entrance/{arrival['id']}".encode()).hexdigest()[:8]
        connection = {
            "id": f"c_{digest}",
            "from": {"map_id": parent_map_id, "node_id": node_id},
            "to": {"map_id": map_id, "node_id": arrival["id"]},
            "kind": str(parsed.get("entrance_kind", "")).strip() or "entrance",
            "name": str(parsed.get("entrance_name", "")).strip(),
            "description": str(parsed.get("entrance_description", "")).strip(),
            "travel": {"mode": "instant"},
            "bidirectional": True,
            "requirements": "",
            "hidden": False,
            "origin": "generated",
        }
        return {"map": record, "connections": [connection]}

    def _build_child_terrain(self, map_id: str, seed: int, name: str,
                             world_id: str = None):
        """Raster stack for one terrain-flagged child map, persisted under the
        world's terrain directory keyed by the child map id — the same key the
        terrain-image route and enrichment sampling use. Returns
        (layers, config_meta); (None, None) when the world can't persist
        terrain (no world id yet) — the map degrades to abstract."""
        persistence = self._services.terrain_store
        if persistence is None or not world_id:
            return None, None
        from wbworldgen.worldgen.terrain_build import build_layer_terrain
        from wbworldgen.worldgen import terrain_store as _ts
        # 256 keeps a lazy planet expansion under ~10s of CPU; the root map's
        # creation-time rasters stay at 1024. Raise the setting for
        # root-quality planets at the cost of slower expansions.
        resolution = self._services.resolve_int_setting(
            "world.child_terrain_resolution", 256, 128, 2048)
        try:
            entry = build_layer_terrain(
                world_id,
                {"layer_id": map_id, "name": name, "layer_type": "surface",
                 "index": seed % 1000},
                resolution, "realistic", persistence)
            layers = _ts.load_terrain(str(persistence.terrain_dir(world_id, map_id)))
        except Exception as e:
            logger.warning("child terrain failed for %s (%s): %s — map degrades "
                           "to abstract", map_id, world_id, e)
            return None, None
        if not layers:
            return None, None
        meta = {"layer_id": map_id, "resolution": entry.get("resolution"),
                "seed": entry.get("seed"), "summary": entry.get("summary", "")}
        return layers, meta

    def _mock_content(self, node: dict, levels: list, max_locations: int,
                      must_list: list = None) -> dict:
        """Deterministic offline content — expansion runs at play time, so it
        must work without a live provider.

        Level pick: a level_type matching the node's type, else the first
        authored (interior-style) level — the pre-procedural default."""
        name = node.get("name", "") or node.get("id", "somewhere")
        node_type = str(node.get("type", "")).strip().lower()
        level = next((l for l in levels if l.get("level_type") == node_type), None)
        if level is None:
            level = next((l for l in levels if _is_authored(l)), levels[0])
        if not _is_authored(level):
            return {
                "label": name,
                "level_type": level.get("level_type", ""),
                "description": f"Mock {level.get('level_type', 'map')} map of {name}.",
                "locations": [],
                "entrance_kind": "arrival",
                "entrance_name": f"{name} Arrival",
            }
        count = min(4, max_locations)
        locations = []
        for i in range(1, count + 1):
            locations.append({
                "name": f"{name} Hall {i}" if i > 1 else f"{name} Gate",
                "type": "gate" if i == 1 else "hall",
                "description": f"Mock area {i} inside {name}.",
                "adjacent": [f"{name} Hall {i - 1}" if i - 1 > 1 else f"{name} Gate"] if i > 1 else [],
                "is_entrance": i == 1,
            })
        have = {l["name"].lower() for l in locations}
        for m in must_list or []:
            if m.get("name") and m["name"].lower() not in have:
                locations.append({
                    "name": m["name"], "type": "room",
                    "description": m.get("description", "") or f"Mock room inside {name}.",
                    "adjacent": [f"{name} Gate"], "is_entrance": False,
                })
        return {
            "label": f"Inside {name}",
            "level_type": level.get("level_type", "interior"),
            "description": f"Mock interior of {name}: {count} connected areas.",
            "locations": locations,
            "entrance_kind": "gate",
            "entrance_name": f"The {name} Gate",
        }

    # --- live LLM call ------------------------------------------------------

    async def _live_expand(self, node: dict, context: dict, parent_map: dict,
                           levels: list, max_locations: int,
                           template_vocab: dict = None,
                           must_include: list = None) -> dict:
        services = self._services
        node_id = node.get("id", "")
        node_name = node.get("name", "Unnamed")
        node_type = node.get("type", "settlement")

        world = context.get("world", {})
        region = context.get("region", {})
        neighbors = [n.get("name") for n in context.get("neighbors", []) if n.get("name")]
        neighbors_line = f"- Nearby on the map: {', '.join(neighbors[:5])}\n" if neighbors else ""
        factions = ", ".join(region.get("factions", [])[:5])
        factions_line = f"- Local factions: {factions}\n" if factions else ""

        sub_noun = "rooms, halls, courts and notable places"
        if isinstance(template_vocab, dict) and template_vocab.get("site_sub_noun"):
            sub_noun = str(template_vocab["site_sub_noun"])
        must_include_line = ""
        if must_include:
            entries = "\n".join(
                f'- "{m.get("name", "")}"'
                + (f" — {m['description']}" if m.get("description") else "")
                for m in must_include)
            must_include_line = (
                f"\nThese places are already established INSIDE {node_name} — the map MUST "
                f"include a location for each:\n{entries}\n")
        if context.get("child_notes"):
            must_include_line += (
                f"\nThe world's creator agreed on design notes about {node_name} — "
                "established facts this map must embody:\n"
                + "\n".join(f"- {n}" for n in context["child_notes"]) + "\n")

        levels_block = "\n".join(
            f"- {l.get('level_type')}: {l.get('guidance', l.get('label', ''))}"
            + ("" if _is_authored(l) else " [procedural — do not author locations]")
            for l in levels)
        procedural_note = ""
        if any(not _is_authored(l) for l in levels):
            procedural_note = (
                "\nIf you choose a level marked [procedural], the map itself is generated "
                "procedurally afterwards: output \"locations\": [] and \"connections\": [] and "
                "provide only label, level_type, a rich description, and the entrance fields — "
                f"how one arrives there from {node_name}'s surroundings (a landing site, a "
                "harbor, a city gate...).\n")

        system = services.prompts(
            "map_expand_system",
            "You are a world-building AI designing one map of a larger world: the interior "
            "or sub-area of a single location, so a storyteller can set scenes inside it. "
            "Ground everything in the provided world and location context. Output ONLY valid JSON.",
        )
        user_msg = services.prompts(
            "map_expand_user",
            f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

Parent map: {parent_map.get('label', '')} ({parent_map.get('level_type', 'world')})
Region context:
- Region: {region.get('name', 'unknown')}
- Terrain: {region.get('terrain', '')}
{factions_line}{neighbors_line}
Location to expand: {node_name} ({node_type})
Description: {node.get('description', '') or node.get('label_description', '')}
{must_include_line}
Choose the level_type for this new map from:
{levels_block}
{procedural_note}
Design 6-{max_locations} distinct locations ({sub_noun}). Exactly ONE location must have
"is_entrance": true — the way in from {node_name}'s surroundings (a gate, door, cave mouth,
docking bay...). Each location gets a name, a short type, a 1-2 sentence description (the
surface — what a visitor perceives), additional_details (1-3 sentences for the storyteller
only: what's really going on here, tensions, a hook; mark genuinely hidden facts with a
leading 'Secret:'), and which other locations it directly adjoins (by name).
You MAY also add further connections out of this map in "connections": each states its kind,
a name, at_location (which of your locations it sits at), to_parent_location (an existing
location name on the parent map, or empty to link back to {node_name} itself), travel
("instant" or a number of turns for a longer crossing), and requirements (empty if open).
A connection may also set "hidden": true — a secret way (a concealed door, a smuggler's
tunnel) the player does not know about until the story uncovers it; when you add one, let
the additional_details of the location it sits at hint at it (marked 'Secret:'). The
entrance itself is never hidden.

Output ONLY valid JSON:
{{"label": "...", "level_type": "...", "description": "2-3 sentences on how this place is laid out",
"entrance_kind": "gate|door|cave mouth|...", "entrance_name": "...", "entrance_description": "...",
"locations": [{{"name": "...", "type": "...", "description": "...", "additional_details": "...", "adjacent": ["..."], "is_entrance": false}}, ...],
"connections": []}}""",
            node_name=node_name,
            node_type=node_type,
            node_description=node.get('description', ''),
            world_name=world.get('name', 'Unknown'),
            world_genre=world.get('genre', ''),
            world_tone=world.get('tone', ''),
            world_premise=world.get('premise', ''),
            max_locations=str(max_locations),
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        await services.backoff.wait()
        async with services.semaphore:
            try:
                return await json_retry_completion(
                    self._llm,
                    messages=messages,
                    model=self._llm.reader_model,
                    temperature=services.temperature or 0.9,
                    inspector_ctx={"call_type": "world_build", "step": "map:expand"},
                    step_label=f"map:expand:{node_id}",
                    retry_attempts=services.json_retry_attempts,
                )
            except Exception as e:
                services.backoff.note_rate_limit(e)
                logger.error("Map expansion failed for node %s: %s", node_id, e)
                raise
