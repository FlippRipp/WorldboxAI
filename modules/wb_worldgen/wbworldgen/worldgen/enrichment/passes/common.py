"""Helpers shared by the built-in enrichment passes: prompt fragments used
by both labeling and describing, and the transient-error retry wrapper every
single-unit LLM call runs under."""

import asyncio
import logging
import re

logger = logging.getLogger(__name__)


def terrain_line(terrain: dict) -> str:
    """One-line terrain fact for the enrichment prompt (empty when unknown)."""
    if not terrain or not terrain.get("biome"):
        return ""
    parts = [f"- Local terrain: {terrain['biome']}"]
    if terrain.get("elevation_band"):
        parts.append(f"({terrain['elevation_band']}")
        near = terrain.get("near_water") or []
        parts[-1] += f", near {', '.join(near)})" if near else ")"
    return " ".join(parts)


# What each inter-layer connection type physically looks like, so generated
# names/descriptions match the kind of passage it actually is.
_CONNECTION_LOOK = {
    "dungeon_entrance": "a dungeon entrance — a dark doorway or descent leading underground",
    "cave_entrance": "a cave mouth opening into the earth",
    "cave_mouth": "a cave mouth opening into the earth",
    "port": "a harbor where ships dock and put to sea",
    "portal": "a magical portal or arcane gateway",
    "rift": "a glowing rift or tear in reality",
    "staircase": "a great staircase linking one level to another",
    "bridge": "a bridge spanning across to another area",
}


def connection_block(connection: dict, vocab: dict = None) -> str:
    """Multi-line note describing the inter-layer connection a node represents,
    so the LLM names/describes it as the right kind of passage. Empty when the
    node is not a layer connection. The world's vocabulary (AI-authored, or a
    template-era snapshot) may add or override connection looks (e.g.
    spaceport/jump_gate for sci-fi)."""
    if not connection:
        return ""
    ctype = connection.get("type", "passage")
    looks = _CONNECTION_LOOK
    if isinstance(vocab, dict) and isinstance(vocab.get("connection_looks"), dict):
        looks = {**_CONNECTION_LOOK, **vocab["connection_looks"]}
    look = looks.get(ctype, f"a {ctype.replace('_', ' ')}")
    parts = [f"This location is a LAYER CONNECTION ({ctype}): {look}."]
    if connection.get("target_layer_id"):
        parts.append(f"It leads to the '{connection['target_layer_id']}' layer.")
    if connection.get("description"):
        parts.append(f"Connection details: {connection['description']}")
    parts.append("Name and describe it as this kind of passage.")
    return " ".join(parts)


def strip_leading_the(name: str) -> str:
    """Drop a leading 'The ' so generated names don't all start the same way."""
    if not name:
        return name
    stripped = re.sub(r'^\s*[Tt]he\s+', '', name).strip()
    return stripped or name.strip()


async def call_with_retries(services, fn, *, what: str, node_id, attempts: int = 3):
    """Run one LLM-call coroutine factory under the shared rate-limit backoff
    and concurrency semaphore, with transient-error retries. Returns the
    call's result, or ``None`` when every attempt failed (already logged)."""
    for attempt in range(attempts):
        try:
            await services.backoff.wait()
            async with services.semaphore:
                return await fn()
        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            logger.warning("Transient error in %s for node %s (attempt %d): %s",
                           what, node_id, attempt + 1, e)
        except Exception as e:
            services.backoff.note_rate_limit(e)
            logger.error("%s failed for node %s: %s", what, node_id, e)
        if attempt < attempts - 1:
            await asyncio.sleep(0.5 * (attempt + 1))
    logger.error("%s exhausted retries for node %s, skipping", what, node_id)
    return None
