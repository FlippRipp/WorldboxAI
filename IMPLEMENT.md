# Enrichment Implementation Order

## Overview
Fixes and improvements to the labeling and description enrichment steps
in the world generation system.

## Order (dependency-aware)

### Phase 1 — Critical Bugs
| # | ID | Task | Files |
|---|----|------|-------|
| 1 | B1 | Fix link syntax triple mismatch | `world_builder.py` |
| 2 | B2 | Fix labeledNodeIds/describedNodeIds confusion | `EnrichmentPanel.jsx` |

### Phase 2 — Robustness
| # | ID | Task | Files |
|---|----|------|-------|
| 3 | Q7 | Fix per-layer progress with layer_filter | `world_builder.py` |
| 4 | Q5 | Build node_id index for O(1) lookups | `world_builder.py` |
| 5 | Q6 | Add TTL eviction to enrichment cache | `world_builder.py` |
| 6 | B3 | Add description output validation | `world_builder.py` |
| 7 | Q9 | Add configurable delay between enrichment calls | `world_builder.py`, `EnrichmentPanel.jsx` |

### Phase 3 — Quality of Life
| # | ID | Task | Files |
|---|----|------|-------|
| 8 | Q4 | Remove unused `generate` field from PipelineStep | `world_builder.py`, `server.py`, `test_world_builder.py` |
| 9 | Q8 | Externalize enrichment prompts | `world_builder.py`, `prompt_library.json` |
| 10 | Q10 | Simplify enrichment data persistence | `world_builder.py`, `server.py` |

### Phase 4 — Verify
| # | ID | Task | Files |
|---|----|------|-------|
| 11 | V | Run tests and linting | `test_world_builder.py`, `test_world_map.py` |

---

## Detailed Implementation Notes

### B1: Link syntax triple mismatch
**Problem:** Three inconsistent formats for neighbor references:
- `_get_neighbor_context` provides `${link_nid}`
- Prompt tells LLM to use `${neighbor_name}`
- `_postprocess_links` regex only matches `${link_nid}`

**Fix:**
- Update prompt to reference `${link_nodeId}` syntax (matches what LLM sees in neighbor context)
- The post-process regex already matches this — no change needed there
- Ensure neighbor context includes both the link_id and name for LLM awareness

### B2: labeledNodeIds confusion
**Problem:** Same `labeledNodeIds` state passed to both label and describe endpoints.
Backend interprets them differently (labeled vs described), causing skipped descriptions.

**Fix:** Split into `labelSessionIds` and `descSessionIds` in EnrichmentPanel.

### Q7: Per-layer progress with filter
**Problem:** When layer_filter is active, only the filtered layer appears in per_layer stats.
Other layers show 0/0, confusing the UI.

**Fix:** Compute full per-layer stats regardless of filter, then emit complete per_layer.

### Q5: O(n) node lookup
**Problem:** `_save_node_enrichment` iterates all layers × all nodes per call.

**Fix:** Build a `_node_index` dict on cache load: `node_id -> (data_ref, node_dict)`.

### Q6: Cache unbounded growth
**Problem:** `_enrichment_cache` never evicts entries.

**Fix:** LRU eviction: max 4 entries, evict oldest on insert.

### B3: Description output validation
**Problem:** Description generation has zero quality checks.

**Fix:** Add min length check (≥ 10 chars) and strip code fences. Retry on failure.

### Q9: Rate limiting
**Problem:** 50ms delay between calls, no server-side guard.

**Fix:** Add `_enrichment_delay_ms` config (default 300ms). Add server-side asyncio.Semaphore(1).

### Q4: Unused `generate` field
**Problem:** Every PipelineStep registered with `generate=None`. Field never read anywhere.

**Fix:** Remove `generate` from dataclass. Update all registration sites and tests.

### Q8: Hardcoded prompts
**Problem:** Enrichment prompts are hardcoded inline strings.

**Fix:** Add template entries to `prompt_library.json`. Load in WorldBuilder with fallback
to hardcoded defaults. Support `{variable}` substitution.

### Q10: Data persistence simplification
**Problem:** Enrichment data duplicated across three locations.

**Fix:** Make `step_map_generation.json` the single source of truth.
`step_node_labeling.json` / `step_node_descriptions.json` are derived summaries (keep them).
Remove the redundant in-memory sync — load from disk on commit instead.
