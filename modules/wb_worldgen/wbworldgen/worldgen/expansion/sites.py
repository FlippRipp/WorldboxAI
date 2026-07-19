"""On-demand site expansion: deep interior detail for major locations.

A "site" is the interior of one major map node (a city, port, stronghold...):
a layout summary plus a handful of sub-locations (districts, venues, notable
places). Sites are deliberately NOT part of the generation pipeline — they are
generated lazily, one full-attention LLM call per site, the first time the
story approaches the location (or on explicit request), then cached forever in
the world's ``sites/`` directory so a site is generated at most once per world.

Sub-locations live under the parent node in the compiled world's additive
``site_maps`` key and never enter the map graph, so travel, fog-of-war, NPC
movement and enrichment are untouched by construction.
"""

import logging

from wbworldgen.worldgen.generation.llm import json_retry_completion
from wbworldgen.worldgen.enrichment.context import build_enrichment_context, collect_nodes_by_layer

logger = logging.getLogger(__name__)

# Node types whose interiors are worth a site of their own. Importance is
# checked separately (see is_expandable) so a lone roadside "settlement" node
# bumped down by the graph pass still qualifies via its authored importance.
EXPANDABLE_TYPES = {"city", "settlement", "port", "stronghold"}

SITE_SCHEMA_VERSION = 1


def is_expandable(node: dict, floor: int = 6) -> bool:
    """Whether a map node qualifies for interior site expansion."""
    if not node or not node.get("name"):
        return False
    return node.get("type", "") in EXPANDABLE_TYPES and node.get("importance", 0) >= floor


def site_world_entries(parent_node_id: str, site: dict) -> list[dict]:
    """RAG world-index entries for a site bundle. Must stay in the same format
    ``memory._build_world_entries`` emits for ``site_maps`` so incremental
    embedding and a later full re-embed agree."""
    entries = []
    parent_name = site.get("name", "")
    if site.get("layout_summary"):
        entries.append({
            "text": f"Layout of {parent_name}: {site['layout_summary']}",
            "source_type": "site",
            "source_id": parent_node_id,
            "region": parent_name,
        })
    for sub in site.get("sub_locations", []):
        if not sub.get("name"):
            continue
        text = f"Place in {parent_name}: {sub['name']} ({sub.get('type', 'place')})"
        if sub.get("description"):
            text += f". {sub['description']}"
        entries.append({
            "text": text,
            "source_type": "site_node",
            "source_id": sub.get("id", ""),
            "region": parent_name,
        })
    return entries


class SiteExpansionEngine:
    """One-call interior expansion for a major location. Shares the
    ``GenServices`` LLM service, prompt library, temperature and
    semaphore/backoff so play-time expansion never competes uncontrolled
    with other enrichment traffic."""

    def __init__(self, services):
        self._services = services

    @property
    def _llm(self):
        return self._services.llm

    async def expand(self, compiled: dict, node: dict, *, max_sub_locations: int = 10,
                     template_vocab: dict = None) -> dict:
        """Generate the site bundle for one node. Raises on LLM failure —
        callers decide whether to retry later; nothing is persisted here."""
        max_sub_locations = max(4, min(int(max_sub_locations or 10), 16))
        if not self._llm or self._llm.mode == "mock":
            return self._mock_site(node, max_sub_locations)

        all_nodes, _ = collect_nodes_by_layer(compiled)
        context = build_enrichment_context(node, all_nodes, compiled, include_descriptions=True)
        parsed = await self._live_expand(node, context, max_sub_locations, template_vocab)
        return self._build_site(node, parsed, max_sub_locations)

    # --- result shaping -----------------------------------------------------

    def _build_site(self, node: dict, parsed: dict, max_sub_locations: int) -> dict:
        node_id = node.get("id", "")
        raw_subs = parsed.get("sub_locations")
        if not isinstance(raw_subs, list) or not raw_subs:
            raise ValueError(f"Site expansion for {node_id} returned no sub_locations")

        # Ids are assigned server-side ("<parent>:s<n>") — never trust LLM ids.
        subs = []
        names_to_id = {}
        for raw in raw_subs[:max_sub_locations]:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            if not name or name.lower() in names_to_id:
                continue
            sid = f"{node_id}:s{len(subs) + 1}"
            names_to_id[name.lower()] = sid
            subs.append({
                "id": sid,
                "name": name,
                "type": str(raw.get("type", "place")).strip() or "place",
                "description": str(raw.get("description", "")).strip(),
                "adjacent": raw.get("adjacent") if isinstance(raw.get("adjacent"), list) else [],
            })
        if not subs:
            raise ValueError(f"Site expansion for {node_id} produced no valid sub_locations")

        # Adjacency arrives as sub-location names; resolve to assigned ids and
        # drop anything unresolvable.
        for sub in subs:
            resolved = []
            for adj in sub["adjacent"]:
                sid = names_to_id.get(str(adj).strip().lower())
                if sid and sid != sub["id"]:
                    resolved.append(sid)
            sub["adjacent"] = resolved

        return {
            "parent_node_id": node_id,
            "name": node.get("name", ""),
            "layout_summary": str(parsed.get("layout_summary", "")).strip(),
            "sub_locations": subs,
            "schema": SITE_SCHEMA_VERSION,
        }

    def _mock_site(self, node: dict, max_sub_locations: int) -> dict:
        """Deterministic offline bundle — site expansion runs at play time, so
        unlike map enrichment it must work without a live provider."""
        node_id = node.get("id", "")
        name = node.get("name", "") or node_id
        count = min(4, max_sub_locations)
        subs = []
        for i in range(1, count + 1):
            subs.append({
                "id": f"{node_id}:s{i}",
                "name": f"{name} Quarter {i}",
                "type": "district",
                "description": f"Mock district {i} of {name}.",
                "adjacent": [f"{node_id}:s{i - 1}"] if i > 1 else [],
            })
        return {
            "parent_node_id": node_id,
            "name": name,
            "layout_summary": f"Mock layout of {name}: {count} districts in a ring.",
            "sub_locations": subs,
            "schema": SITE_SCHEMA_VERSION,
        }

    # --- live LLM call ------------------------------------------------------

    async def _live_expand(self, node: dict, context: dict, max_sub_locations: int,
                           template_vocab: dict = None) -> dict:
        services = self._services
        node_id = node.get("id", "")
        node_name = node.get("name", "Unnamed")
        node_type = node.get("type", "settlement")

        world = context.get("world", {})
        region = context.get("region", {})
        layer = context.get("layer", {})
        neighbors = [n.get("name") for n in context.get("neighbors", []) if n.get("name")]

        named_locations = []
        for nl in (region.get("landmarks") or [])[:6]:
            named_locations.append(str(nl))
        factions = ", ".join(region.get("factions", [])[:5])
        factions_line = f"- Local factions: {factions}\n" if factions else ""
        neighbors_line = f"- Nearby on the map: {', '.join(neighbors[:5])}\n" if neighbors else ""
        landmarks_line = f"- Regional landmarks: {', '.join(named_locations)}\n" if named_locations else ""

        sub_noun = "districts, streets, venues and notable places"
        if isinstance(template_vocab, dict) and template_vocab.get("site_sub_noun"):
            sub_noun = str(template_vocab["site_sub_noun"])

        system = services.prompts(
            "site_expand_system",
            "You are a world-building AI. Expand one major location into its interior detail: "
            "a compact layout overview and its distinct sub-locations, so a storyteller can set "
            "scenes inside it. Ground everything in the provided world, region and location "
            "context. Output ONLY valid JSON.",
        )
        user_msg = services.prompts(
            "site_expand_user",
            f"""World: {world.get('name', 'Unknown')} ({world.get('genre', '')}, {world.get('tone', '')})
World premise: {world.get('premise', '')}

Region context:
- Region: {region.get('name', 'unknown')}
- Terrain: {region.get('terrain', '')}
- Climate: {region.get('climate', '')}
{factions_line}{landmarks_line}{neighbors_line}
Location to expand: {node_name} ({node_type})
Layer: {layer.get('name', 'surface')} ({layer.get('type', 'surface')})
Description: {node.get('description', '') or node.get('label_description', '')}

Design the interior of {node_name}: its overall layout and 6-{max_sub_locations} distinct sub-locations ({sub_noun}).
Each sub-location gets a name, a short type (e.g. district, market, tavern, docks, temple), a 1-2 sentence
description, and which other sub-locations it directly adjoins (by name).

Output ONLY valid JSON:
{{"layout_summary": "2-3 sentences describing how {node_name} is laid out",
"sub_locations": [{{"name": "...", "type": "...", "description": "...", "adjacent": ["..."]}}, ...]}}""",
            world_name=world.get('name', 'Unknown'),
            world_genre=world.get('genre', ''),
            world_tone=world.get('tone', ''),
            world_premise=world.get('premise', ''),
            node_name=node_name,
            node_type=node_type,
            node_description=node.get('description', ''),
            region_name=region.get('name', 'unknown'),
            region_terrain=region.get('terrain', ''),
            region_climate=region.get('climate', ''),
            max_sub_locations=str(max_sub_locations),
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
                    inspector_ctx={"call_type": "world_build", "step": "site:expand"},
                    step_label=f"site:expand:{node_id}",
                    retry_attempts=services.json_retry_attempts,
                )
            except Exception as e:
                services.backoff.note_rate_limit(e)
                logger.error("Site expansion failed for node %s: %s", node_id, e)
                raise
