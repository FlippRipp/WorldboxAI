"""The enrichment scheduler: runs registered passes over a compiled world.

B1 of the worldgen architecture plan: the engine no longer knows what
labeling or describing *is* — passes are ``PassSpec`` catalog entries
(``registry.py``) whose prompts and LLM calls live in ``passes/``. What
stays here is everything genuinely shared: importance ordering, per-unit
pending computation (normal / rework / scoped runs), bounded concurrency,
batching for batchable specs, flush cadence, cancellation, SSE progress
events, compiled-cache handling, and trigger firing (review runs over each
map whose naming a run completes). One scheduler, two iteration shapes —
node passes and map passes.

Dependencies (LLM, enrichment store, compiled-world cache, prompt
templates, throttling) are the named fields of a ``GenServices`` object
built by the WorldBuilder facade.
"""

import asyncio
import logging

from wbworldgen.worldgen.enrichment.context import collect_nodes_by_layer
from wbworldgen.worldgen.enrichment.registry import (
    RunState,
    node_passes,
    registered_passes,
    resolve_phases,
)
import wbworldgen.worldgen.enrichment.passes  # noqa: F401 — registers the built-ins

logger = logging.getLogger(__name__)


class EnrichmentEngine:
    def __init__(self, services):
        self._services = services
        self._cancel_flags: set = set()

    @property
    def _llm(self):
        return self._services.llm

    def _load_compiled(self, world_id: str) -> dict:
        return self._services.compiled.load(world_id)

    def cancel(self, world_id: str):
        """Request cancellation of an in-flight run for this world (checked
        between work items; already-saved results are kept and flushed)."""
        self._cancel_flags.add(world_id)

    # --- pending computation --------------------------------------------------

    def _pending_for_pass(self, spec, all_nodes: list, layer_map: dict,
                          layer_filter: str, rework: bool,
                          importance_floor: int = None,
                          node_ids: list = None) -> tuple:
        """Work queue + progress baseline for one node pass.

        Returns (pending, per_layer, done, total) where pending is importance-
        sorted, per_layer mirrors the shape the frontend progress bars consume,
        and done/total are derived from the spec's ``is_done``/``in_domain``
        predicates: total counts the pass's domain, done counts nodes already
        carrying its output. A rework run revisits nodes whose output already
        exists instead of skipping them.

        ``node_ids`` narrows the run to an explicit target set (play-time
        backfill of specific nodes) and takes precedence over
        ``importance_floor``, which narrows it to major locations
        (importance >= floor). When either is set, done/total and the per-layer
        counters are scoped to the targeted nodes so progress reads as complete
        when the targeted work is done."""
        in_scope = [n for n in all_nodes
                    if not layer_filter
                    or n.get("map_id", n.get("layer_id", "")) == layer_filter]
        scoped = node_ids is not None or importance_floor is not None
        if node_ids is not None:
            wanted = {str(nid) for nid in node_ids}
            in_scope = [n for n in in_scope if str(n.get("id")) in wanted]
        elif importance_floor is not None:
            in_scope = [n for n in in_scope if n.get("importance", 0) >= importance_floor]

        domain = spec.in_domain
        is_done = spec.is_done
        if rework:
            pending = [n for n in in_scope if domain(n) and is_done(n)]
            total = len(pending)
            done = 0
        elif scoped:
            pending = [n for n in in_scope if domain(n) and not is_done(n)]
            total = sum(1 for n in in_scope if domain(n))
            done = sum(1 for n in in_scope if is_done(n))
        else:
            pending = [n for n in in_scope if domain(n) and not is_done(n)]
            total = sum(1 for n in all_nodes if domain(n))
            done = sum(1 for n in all_nodes if is_done(n))

        count_pool = in_scope if scoped else all_nodes
        per_layer = {}

        def _map_key(n):
            return n.get("map_id", n.get("layer_id", "")) or "main"

        for lid, info in layer_map.items():
            lid_done = 0 if rework else sum(
                1 for n in count_pool
                if _map_key(n) == (lid or "main") and is_done(n)
            )
            lid_total = info["total"] if not scoped else sum(
                1 for n in in_scope if _map_key(n) == (lid or "main")
            )
            per_layer[lid] = {"done": lid_done, "total": lid_total}

        pending.sort(key=lambda n: -n.get("importance", 0))
        return pending, per_layer, done, total

    def _maps_in_scope(self, compiled: dict, layer_filter: str = None,
                       map_ids: list = None) -> list:
        """Map records a map pass runs over, in map-catalog order."""
        from wbworldgen.worldgen import mapspace as _ms
        wanted = {str(m) for m in map_ids} if map_ids is not None else None
        recs = []
        for mid, rec in _ms.maps_by_id(compiled).items():
            if wanted is not None and mid not in wanted:
                continue
            if layer_filter and mid != layer_filter:
                continue
            recs.append(rec)
        return recs

    # --- map passes -----------------------------------------------------------

    async def _run_map_pass(self, spec, state: RunState, maps: list):
        """Run one map pass over ``maps`` sequentially, merging each map's
        summary contribution (numbers add, lists extend). Returns the
        aggregate dict, or None when no map produced one."""
        agg = None
        for rec in maps:
            if state.world_id in self._cancel_flags:
                break
            contribution = await spec.run(self._services, rec, state)
            if contribution is None:
                continue
            if agg is None:
                agg = {}
            for k, v in contribution.items():
                if isinstance(v, list):
                    agg.setdefault(k, []).extend(v)
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    agg[k] = agg.get(k, 0) + v
                else:
                    agg[k] = v
        return agg

    async def _fire_triggers(self, spec, pending: list, state: RunState,
                             summary: dict):
        """After a node pass finishes its phase, run every map pass whose
        trigger watches it — over each map this run's work completed (every
        node in the map now satisfies the triggering pass's ``is_done``).
        Best-effort: a triggered pass failing never fails the run."""
        if not summary.get(spec.summary_field):
            return
        for t_spec in registered_passes():
            if (t_spec.triggers or {}).get("on_map_complete") != spec.id:
                continue

            def _mkey(n):
                return n.get("map_id", n.get("layer_id", ""))

            touched = {_mkey(n) for n in pending}
            completed = [mid for mid in touched if all(
                spec.is_done(n) for n in state.all_nodes if _mkey(n) == mid)]
            if not completed:
                continue
            try:
                maps = self._maps_in_scope(state.compiled, map_ids=completed)
                agg = await self._run_map_pass(t_spec, state, maps)
                if agg is not None:
                    summary[t_spec.summary_field] = agg
            except Exception:
                logger.warning("Triggered %s pass failed for %s", t_spec.id,
                               state.world_id, exc_info=True)

    # --- the run --------------------------------------------------------------

    async def run(self, world_id: str, phase: str = "all", count: int = None,
                  layer_filter: str = None, rework: bool = False,
                  exclude_node_ids: list = None, concurrency: int = 3,
                  batch_size: int = 8, on_event=None,
                  importance_floor: int = None, node_ids: list = None,
                  guidance: str = None, specs: list = None) -> dict:
        """Run enrichment passes in one server-driven run with bounded
        concurrency.

        ``phase`` is a registered pass id ("label", "describe", "review", ...)
        or "all" (every non-triggered pass in dependency order); unknown ids
        fail loudly. The compiled world + terrain rasters are loaded once for
        the whole run instead of per node. Progress is reported through
        ``on_event`` (async callable) as {"type": "node"|"failed"|"phase"|
        "done", ...} dicts, with the "phase" field carrying the pass id.
        Results are write-cached per node and flushed to disk every few nodes,
        at phase end and on cancellation.

        ``importance_floor`` limits node passes to major locations
        (importance >= floor); ``node_ids`` limits them to an explicit target
        set and wins over the floor. See ``_pending_for_pass``. Map passes
        (review) run over the maps in scope and ignore both.

        ``guidance`` is the run-level steering note handed to every pass via
        ``RunState.guidance`` (C1's guidance channel). ``specs`` runs an
        explicit spec list instead of resolving ``phase`` against the
        registry — the ad-hoc capability path (`pass:custom` builds a
        per-run PassSpec); everything else about the run is identical.
        """
        if not self._llm or self._llm.mode == "mock":
            raise RuntimeError("Enrichment requires an LLM service. The mock enrichment has been removed.")
        specs = resolve_phases(phase) if specs is None else list(specs)

        async def emit(evt: dict):
            if on_event is None:
                return
            try:
                await on_event(evt)
            except Exception:
                logger.warning("enrichment run event callback failed", exc_info=True)

        concurrency = max(1, int(concurrency))
        self._cancel_flags.discard(world_id)
        compiled = self._load_compiled(world_id)
        # One canonical node list for the whole run: node dicts are mutated in
        # memory as work lands so later passes see fresh neighbor names
        # without re-loading the world from disk.
        all_nodes, layer_map = collect_nodes_by_layer(compiled)
        state = RunState(world_id=world_id, compiled=compiled,
                         all_nodes=all_nodes, layer_map=layer_map,
                         rework=rework, emit=emit, guidance=guidance or "")

        summary = {s.summary_field: 0 for s in node_passes()}
        summary["failed_node_ids"] = []
        summary["cancelled"] = False
        flush_pending = 0

        try:
            for spec in specs:
                if spec.unit == "map":
                    maps = self._maps_in_scope(compiled, layer_filter=layer_filter)
                    if count is not None:
                        maps = maps[:max(0, int(count))]
                    await emit({"type": "phase", "phase": spec.id,
                                "pending": len(maps), "total_labeled": 0,
                                "total_nodes": len(maps), "per_layer": {}})
                    if maps:
                        agg = await self._run_map_pass(spec, state, maps)
                        if agg is not None:
                            summary[spec.summary_field] = agg
                    if world_id in self._cancel_flags:
                        summary["cancelled"] = True
                        break
                    continue

                pending, per_layer, done, total = self._pending_for_pass(
                    spec, all_nodes, layer_map, layer_filter, rework,
                    importance_floor=importance_floor, node_ids=node_ids)
                if exclude_node_ids:
                    skip = set(exclude_node_ids)
                    pending = [n for n in pending if n.get("id") not in skip]
                if count is not None:
                    pending = pending[:max(0, int(count))]
                await emit({"type": "phase", "phase": spec.id, "pending": len(pending),
                            "total_labeled": done, "total_nodes": total,
                            "per_layer": per_layer})
                if not pending:
                    continue

                queue = asyncio.Queue()
                if spec.batchable and batch_size > 1:
                    # Batched work: several nodes per LLM call. Invalid or
                    # duplicate entries get re-queued as single nodes.
                    for i in range(0, len(pending), batch_size):
                        queue.put_nowait(pending[i:i + batch_size])
                else:
                    for n in pending:
                        queue.put_nowait(n)

                event_of = spec.event_fields or (lambda fields: dict(fields))

                def progress_snapshot():
                    # Copy the shared counters: events sit in the SSE queue while
                    # other workers keep mutating per_layer.
                    return {"total_labeled": done, "total_nodes": total,
                            "per_layer": {lid: dict(v) for lid, v in per_layer.items()}}

                async def record_result(node, event_fields: dict):
                    nonlocal done, flush_pending
                    done += 1
                    lid = node.get("map_id", node.get("layer_id", ""))
                    layer_key = lid if lid in per_layer else "main"
                    if layer_key in per_layer:
                        per_layer[layer_key]["done"] += 1
                    flush_pending += 1
                    if flush_pending >= 10:
                        flush_pending = 0
                        self._services.enrichment_store.flush_enrichment_cache(world_id)
                    await emit({"type": "node", "phase": spec.id, "node_id": node.get("id"),
                                "layer_id": lid, **event_fields, **progress_snapshot()})

                async def record_failure(node):
                    summary["failed_node_ids"].append(node.get("id"))
                    await emit({"type": "failed", "phase": spec.id, "node_id": node.get("id"),
                                "layer_id": node.get("layer_id", ""), **progress_snapshot()})

                def store_fields(node, fields: dict):
                    # Persist truthy fields through the enrichment store,
                    # mirror them onto the compiled cache and the run's
                    # in-memory node so later work sees them.
                    node_id = node.get("id")
                    for f, v in fields.items():
                        if not v:
                            continue
                        self._services.enrichment_store.save_node_enrichment(world_id, node_id, f, v)
                        self._services.compiled.update_node(compiled, node_id, f, v)
                        node[f] = v
                    summary[spec.summary_field] = summary.get(spec.summary_field, 0) + 1

                async def worker():
                    while world_id not in self._cancel_flags:
                        try:
                            item = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return

                        if isinstance(item, list):
                            results, leftovers = await spec.run_batch(
                                self._services, item, state)
                            for node in item:
                                fields = results.get(node.get("id"))
                                if fields is None:
                                    continue
                                store_fields(node, fields)
                                await record_result(node, event_of(fields))
                            for node in leftovers:
                                queue.put_nowait(node)
                            continue

                        node = item
                        fields = await spec.run(self._services, node, state)
                        if fields is None:
                            await record_failure(node)
                            continue
                        store_fields(node, fields)
                        await record_result(node, event_of(fields))

                await asyncio.gather(*(worker() for _ in range(min(concurrency, queue.qsize()))))
                self._services.enrichment_store.flush_enrichment_cache(world_id)
                flush_pending = 0
                if world_id in self._cancel_flags:
                    summary["cancelled"] = True
                    break

                await self._fire_triggers(spec, pending, state, summary)
        finally:
            if flush_pending:
                self._services.enrichment_store.flush_enrichment_cache(world_id)
            self._cancel_flags.discard(world_id)
            # Keep the cheap compiled JSON cached but free the decompressed
            # rasters (tens of MB — matters on Termux).
            self._services.compiled.release_terrain(world_id)

        await emit({"type": "done", **summary})
        return summary
