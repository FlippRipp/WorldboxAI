"""Silent background backfill. Worlds created in "major_locations" mode leave
ordinary waypoints unnamed/undescribed; during play they are detailed on
demand — nodes the story approaches first (fog reveal, travel routes,
arrival), then a low-priority idle trickle over the rest. All generation
goes through one serialized worker so runs never overlap, and every result
is synced into the live session, the save's world_data.json and the RAG
world index. Node detail is generated at most once per world.

Queue/worker state lives on the host (backend module): ``host._backfill`` is
a dict with keys task/queue/queued/failed/futures/disabled, ``host._site_tasks``
holds in-flight expansion tasks.
"""

import asyncio
import logging

from . import sync as _sync
from .worldspace import all_map_nodes, get_travel, node_needs_detail

_logger = logging.getLogger(__name__)


def backfill_reset(host):
    """Forget session-scoped backfill state (tests / world switch)."""
    host._backfill["task"] = None
    host._backfill["queue"] = []
    host._backfill["queued"] = set()
    host._backfill["failed"] = set()
    host._backfill["futures"] = {}
    host._backfill["disabled"] = False
    host._site_tasks.clear()


def backfill_available(host, state: dict) -> bool:
    if host._backfill["disabled"] or host.world_builder is None or host._services is None:
        return False
    if not state.get("world_id"):
        return False
    llm = getattr(host._services.get("engine"), "llm", None)
    return llm is not None and getattr(llm, "mode", "mock") != "mock"


def backfill_per_turn(host) -> int:
    try:
        if host._services is not None and host._services.get("settings") is not None:
            return max(0, int(host._services["settings"].get("world.backfill_per_turn")))
    except Exception:
        pass
    return 2


def queue_backfill(host, state: dict, node_ids: list, front: bool = False):
    """Queue nodes for background detailing and make sure the worker runs."""
    if not backfill_available(host, state):
        return
    fresh = [nid for nid in node_ids
             if nid and nid not in host._backfill["queued"] and nid not in host._backfill["failed"]]
    if fresh:
        if front:
            host._backfill["queue"][:0] = fresh
        else:
            host._backfill["queue"].extend(fresh)
        host._backfill["queued"].update(fresh)
    if host._backfill["queue"] and (host._backfill["task"] is None or host._backfill["task"].done()):
        host._backfill["task"] = asyncio.create_task(backfill_worker(host, state.get("world_id")))


async def backfill_worker(host, world_id: str):
    """Drain the queue in small chunks: enrich via the world builder (writes
    the world template files), then sync results into the live session."""
    try:
        while host._backfill["queue"]:
            chunk = host._backfill["queue"][:4]
            del host._backfill["queue"][:len(chunk)]
            try:
                summary = await host.world_builder.detail_nodes(world_id, chunk)
                host._backfill["failed"].update(summary.get("failed_node_ids", []))
                _sync.sync_enriched_nodes(host, world_id, chunk)
                await _sync.embed_backfilled_nodes(host, world_id, chunk)
            except FileNotFoundError:
                # The world template dir is gone — nothing to generate from.
                host._backfill["disabled"] = True
                _logger.warning("world '%s' missing; background detail disabled", world_id)
                return
            except Exception:
                host._backfill["failed"].update(chunk)
                _logger.exception("background detail failed for nodes %s", chunk)
            finally:
                for nid in chunk:
                    host._backfill["queued"].discard(nid)
                    fut = host._backfill["futures"].pop(nid, None)
                    if fut is not None and not fut.done():
                        fut.set_result(True)
    finally:
        host._backfill["task"] = None


async def ensure_current_node_detailed(host, state: dict):
    """Await-on-arrival: if the player stands on an undetailed node, wait
    (bounded) for its detail so the storyteller never narrates a fresh scene
    from thin air. Everything else stays non-blocking; on timeout the
    generation keeps running in the background and lands next turn."""
    world_data = state.get("world_data")
    node_id = state.get("player_location_node_id")
    if not world_data or not node_id or not backfill_available(host, state):
        return
    if get_travel(state):
        return  # en route — the journey narration doesn't need the destination yet
    node = next((n for n in all_map_nodes(world_data) if n.get("id") == node_id), None)
    if node is None or not node_needs_detail(node) or node_id in host._backfill["failed"]:
        return
    fut = host._backfill["futures"].get(node_id)
    if fut is None:
        fut = asyncio.get_running_loop().create_future()
        host._backfill["futures"][node_id] = fut
    queue_backfill(host, state, [node_id], front=True)
    try:
        await asyncio.wait_for(asyncio.shield(fut), timeout=20)
    except asyncio.TimeoutError:
        _logger.warning("timed out waiting for detail of node %s; continuing with sparse context", node_id)
    except Exception:
        pass


def kick_background_detail(host, state: dict):
    """Fire-and-forget per-turn triggers: prefetch along an active travel
    route, then top up the idle trickle with the most important pending nodes."""
    from . import expansion as _expansion
    world_data = state.get("world_data")
    if not world_data or not backfill_available(host, state):
        return
    all_nodes = all_map_nodes(world_data)
    by_id = {n.get("id"): n for n in all_nodes}

    # Travel prefetch: destination and remaining waypoints, highest priority —
    # multi-turn journeys hide the generation latency entirely.
    travel = get_travel(state)
    if travel:
        route = travel.get("route", [])
        ahead = route[travel.get("leg_index", 0) + 1:]
        dest = travel.get("destination_node_id")
        wanted = ([dest] if dest else []) + list(ahead)
        needs = [nid for nid in wanted
                 if nid in by_id and node_needs_detail(by_id[nid])]
        if needs:
            queue_backfill(host, state, needs, front=True)
        if _expansion.site_mode(host) == "prefetch" and dest:
            # Start the destination's interior while the journey plays out.
            _expansion.maybe_expand_site(host, state, dest)

    # Idle trickle: keep quietly finishing the world, visited areas first.
    per_turn = backfill_per_turn(host)
    if per_turn <= 0 or host._backfill["queue"]:
        return
    pending = [n for n in all_nodes
               if node_needs_detail(n) and n.get("id") not in host._backfill["failed"]
               and n.get("id") not in host._backfill["queued"]]
    if not pending:
        return
    revealed = set(state.get("revealed_node_ids", []))
    pending.sort(key=lambda n: (-(n.get("id") in revealed), -n.get("importance", 0)))
    queue_backfill(host, state, [n["id"] for n in pending[:per_turn]])
