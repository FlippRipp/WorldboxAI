"""Explicit service contract between the WorldBuilder facade and the
generation engines.

``GenServices`` is the one object the engines (enrichment, site expansion,
map expansion) receive instead of the facade itself: every dependency an
engine may touch is a named field here, so the contract is readable in one
place and a test can hand an engine a hand-built fake instead of
monkeypatching facade privates.

The facade owns the instance and keeps the mutable fields current (``llm``
on ``set_llm_service``, ``temperature`` on ``set_world_builder_temperature``,
``semaphore`` when a run resizes concurrency); engines always read through
the services object, never cache the values.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from wbworldgen.worldgen.compiled_cache import CompiledWorldCache

# Substrings that identify a provider rate-limit error; when one is seen all
# in-flight generation workers back off together instead of hammering the API.
_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "quota",
                       "resource_exhausted", "too many requests")


class RateLimitBackoff:
    """Shared cool-down for provider rate limits.

    One instance is shared by every engine so a 429 seen by any LLM call
    pauses them all. ``note_rate_limit`` arms the cool-down when the error
    looks like a rate limit; ``wait`` blocks until it has passed.
    """

    def __init__(self):
        self._until: float = 0.0

    def note_rate_limit(self, exc) -> bool:
        msg = str(exc).lower()
        if any(marker in msg for marker in _RATE_LIMIT_MARKERS):
            self._until = max(self._until, time.monotonic() + 5.0)
            return True
        return False

    async def wait(self):
        while True:
            delay = self._until - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 5.0))


@dataclass
class GenServices:
    """Everything a generation engine may need, as named fields.

    ``enrichment_store`` and ``terrain_store`` are both the WorldPersistence
    instance in production; they are separate fields because they are
    separate contracts — a fake needs only the methods listed for the field.
    """

    #: Live LLM service (``simple_completion`` etc.); None or ``mode ==
    #: "mock"`` means offline — engines fall back to their mock content.
    llm: Any = None
    #: Prompt library lookup: ``prompts(prompt_id, fallback, **kwargs) -> str``.
    prompts: Callable[..., str] = None
    #: Enrichment write path: ``save_node_enrichment(world_id, node_id,
    #: field, value)`` and ``flush_enrichment_cache(world_id=None)``; the
    #: start-location authoring path additionally uses
    #: ``append_map_node(world_id, map_id, node, edges)``.
    enrichment_store: Any = None
    #: Compiled-world cache shared by every engine (see compiled_cache.py).
    compiled: CompiledWorldCache = None
    #: ``load_world(world_id) -> world_state`` (fresh read, uncached).
    load_world: Callable[[str], dict] = None
    #: Terrain raster access: ``terrain_dir(world_id, layer_id="") -> Path``.
    #: Best-effort — None disables terrain sampling and child-terrain builds.
    terrain_store: Any = None
    #: Live integer-setting read: ``resolve_setting(key, default, lo, hi)``.
    resolve_setting: Callable[[str, int, int, int], int] = None
    #: World-builder LLM temperature; None means each call's own default.
    temperature: Optional[float] = None
    #: Retry budget for JSON-mode LLM calls.
    json_retry_attempts: int = 2
    #: Global ceiling on concurrent generation LLM calls. Replaced wholesale
    #: when concurrency is resized; in-flight holders release on the object
    #: they acquired, so swapping mid-flight is safe.
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(3))
    #: Shared rate-limit cool-down (see RateLimitBackoff).
    backoff: RateLimitBackoff = field(default_factory=RateLimitBackoff)

    def resolve_int_setting(self, key: str, default: int, lo: int, hi: int) -> int:
        """``resolve_setting`` with a safe fallback when no resolver is wired."""
        if self.resolve_setting is None:
            return max(lo, min(default, hi))
        return self.resolve_setting(key, default, lo, hi)
