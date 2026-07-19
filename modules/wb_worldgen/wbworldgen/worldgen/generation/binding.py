"""Bind authored named locations onto generated map nodes.

Works on the plain-dict node/edge shape (post-``to_dict``), so any generator's
output can be bound. Used by the overworld path (``generation/maps.py``), the
city generator and child-map expansion.
"""

from wbworldgen.mapmodel import join_key


def bind_named_locations(nodes: list, named_locations: list,
                         edges: list = None) -> int:
    """Stamp authored named locations onto the most important unnamed nodes.

    world_format 2 path (no regions): settlements bind as type=settlement with
    importance floored to 8, landmarks as type=landmark floored to 6 — the
    same floors the region-based ``_bind`` used. ``nodes`` are plain dicts
    (post-``to_dict``). Returns how many locations were bound (including
    contained attachments).

    Anchoring: a location with ``part_of`` follows the named place it belongs
    to instead of binding independently. ``relation`` "adjacent" binds it to
    the free node closest (graph hops over ``edges``) to its anchor;
    "inside" creates no node at all — the location is recorded on the anchor
    node's ``contained_locations``, guaranteed to appear when the anchor is
    expanded. Anchors resolve against already-named nodes AND locations bound
    earlier in the same call, so anchor chains work regardless of list order.
    A location whose name is already on the map is skipped (it was placed by
    an earlier pass — e.g. region-based geometric placement). All name
    matching — anchors, dedup, region labels — is case- and leading-article-
    tolerant (``join_key``)."""
    if not named_locations:
        return 0

    _key = join_key

    by_name = {_key(n.get("name")): n for n in nodes if n.get("name")}
    free = sorted((n for n in nodes if not n.get("name")),
                  key=lambda n: -n.get("importance", 0))

    adjacency: dict = {}
    for e in edges or []:
        a, b = e.get("from"), e.get("to")
        if a and b:
            adjacency.setdefault(a, []).append(b)
            adjacency.setdefault(b, []).append(a)

    def _stamp(node, loc, node_type, floor):
        node["name"] = loc.get("name", "")
        node["type"] = node_type
        if loc.get("description"):
            node["description"] = loc["description"]
        node["importance"] = max(node.get("importance", 0), floor)
        by_name[_key(node["name"])] = node

    def _nearest_free(anchor_node):
        """Closest unnamed node to the anchor by graph hops (BFS); highest
        importance wins within the same hop distance."""
        free_ids = {n.get("id"): n for n in free}
        seen = {anchor_node.get("id")}
        frontier = [anchor_node.get("id")]
        while frontier:
            ring = []
            for nid in frontier:
                for nb in adjacency.get(nid, []):
                    if nb not in seen:
                        seen.add(nb)
                        ring.append(nb)
            hits = [free_ids[nid] for nid in ring if nid in free_ids]
            if hits:
                return max(hits, key=lambda n: n.get("importance", 0))
            frontier = ring
        return None

    pending = [l for l in named_locations if _key(l.get("name")) not in by_name]
    standalone = [l for l in pending if not l.get("part_of")]
    anchored = [l for l in pending if l.get("part_of")]

    bound = 0

    def _floor_for(loc):
        return ("settlement", 8) if loc.get("category") == "settlement" \
            else ("landmark", 6)

    def _take_free(loc):
        """Most important free node, preferring the location's own region
        when the map's nodes carry region labels."""
        if not free:
            return None
        region = _key(loc.get("region"))
        if region:
            node = next((n for n in free if _key(n.get("region")) == region), None)
            if node is not None:
                free.remove(node)
                return node
        return free.pop(0)

    for loc in sorted(standalone, key=lambda l: _floor_for(l)[0] != "settlement"):
        node = _take_free(loc)
        if node is None:
            break
        node_type, floor = _floor_for(loc)
        _stamp(node, loc, node_type, floor)
        bound += 1

    for loc in anchored:
        anchor = by_name.get(_key(loc.get("part_of")))
        node_type, floor = _floor_for(loc)
        if anchor is not None and loc.get("relation") == "inside":
            contained = anchor.setdefault("contained_locations", [])
            if _key(loc.get("name")) not in {_key(c.get("name")) for c in contained}:
                contained.append({"name": loc.get("name", ""),
                                  "description": loc.get("description", "")})
                bound += 1
            continue
        node = _nearest_free(anchor) if anchor is not None else None
        if node is not None:
            free.remove(node)
        else:
            # No resolvable anchor (or no reachable free node): bind like a
            # standalone location rather than dropping authored content.
            node = _take_free(loc)
            if node is None:
                continue
        _stamp(node, loc, node_type, floor)
        bound += 1
    return bound
