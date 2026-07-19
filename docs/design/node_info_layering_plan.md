# Node Information Layering — Surface vs Storyteller Details

*Status: DESIGNED 2026-07-19 with Filip; all three slices LANDED the same
day (slice 1 model+writers 87c9608, slice 2 context flow 505bbda, slice 3
hidden connections cca663a — refinements recorded per slice below).
Outstanding: the Arc-B-style live verification (folds into Filip's
outstanding agent-mode live test). Records the
decisions from the conversation so implementation starts from decisions,
not re-derivation. Companion docs:
`docs/design/worldgen_agentic_architecture_plan.md` (the pass registry,
agent tools and design principles cited here),
`docs/systems/hierarchy.md` (node data contract — updates when this
lands). Principle citations (P1, P7, P8, P9) refer to the Design
principles section of the agentic architecture plan.*

## The problem

A node has one prose field, `description`, and it serves three audiences
at once: the player's map UI renders it verbatim once the node is
revealed (`MapRenderer.jsx` node-info panel; `GameMapOverlay.jsx` prints
sub-location descriptions), the storyteller LLM receives it in
`<current_location>` with instructions to weave the surroundings into
narration, and the enrichment/known-locations machinery reads it as
ground truth. So the moment the player steps on a node, everything the
world knows about it is exposed — a trap or a secret door authored into
the description is narrated on arrival or literally readable off the map
panel.

Two pieces of code already show the strain:

- The auto-reveal pass infers secrecy *from prose* — its prompt tells
  the LLM the character does not know "places whose description marks
  them as secret, hidden, lost or undiscovered"
  (`wbruntime/known_locations.py`). A structured channel would make that
  judgement grounded instead of stylistic.
- The runtime has a complete, live-verified secrecy discipline for
  connections — the `hidden` flag, the never-volunteer SECRET line in
  the movement primer, the `discover_passage` reveal tool — and
  generation never produces a secret for it: every generated connection
  hardcodes `hidden: False` (`expansion/maps_expand.py`), and the v2a
  surgery tool `add_connection` does not expose the flag. The machinery
  is dormant.

The fix is not hiding descriptions better — they are *supposed* to be
shown. It is giving the rest of the information a home that is not the
public field, and generalizing the connection-secrecy trust model to
content.

## Decisions (settled with Filip, 2026-07-19)

**N1 — Every detailed node carries two text channels.**

- `description` stays what it is: the surface — what a visitor standing
  there perceives (sight, sound, smell; the describe prompt gains that
  sensory framing explicitly). UI-visible, link-tokened, unchanged in
  meaning.
- `additional_details` (Filip's name for it) is new: storyteller-facing
  depth — a few more sentences of flavor, history, inhabitants,
  tensions, story hooks, and secrets. Never rendered by any player UI.
  Not a sparse extra for special nodes: **every node that gets a
  description gets details**, produced by the same call (decided
  against the sparse/optional alternative — depth everywhere beats
  rationed secrets, and secrets you don't use are cheap).

Sub-locations get the same pair (site expansion already returns JSON;
each sub-location gains `additional_details`, and the site bundle gains
a site-level one beside `layout_summary`).

**N2 — Secrets by convention, leak-tolerant.** Inside
`additional_details`, facts genuinely unknown to bystanders are marked
with a leading `Secret:`. The location-context primer tells the
storyteller: unmarked details are yours to weave in freely; marked
secrets are revealed only when the fiction earns them. That is the same
trust model as the SECRET exits, which held up in live verification.
Explicitly accepted (Filip): no hard gating, no redaction — if the
storyteller judges a reveal makes sense, that is the system working, not
a leak. No discovered/undiscovered state is tracked in v1; a revealed
fact lives in the story text.

**N3 — Details ride the existing calls; the call count stays flat.**
(Choice delegated to Claude.) No new enrichment pass. The describe call
switches from plain text to a JSON completion returning
`{"description", "additional_details"}` (via the app-standard
`json_retry_completion` hardening); site expansion adds the fields to
its existing bundle call. Rationale: upfront enrichment cost stays flat
(~54 calls stayed ~54 on the Shattered Sea reference world instead of
doubling), play-time backfill latency stays one call for a
walked-onto node, surface and depth are written together so they
cohere, and review repairs — which re-invoke describe — rework both
automatically. The B1 pass registry would have made a dedicated pass a
file-drop, but two calls per node buys nothing here; a dedicated
secrets pass is the recorded fallback if ride-along depth proves
shallow (see v2).

Every authoring surface that writes node descriptions adds the field to
its output: the describe pass, site expansion, LLM child-map expansion,
and start-location authoring (`author_location`). Implementation
enumerates and covers all of them — none may silently stay
surface-only (P1 spirit: the field is part of the node contract, not a
special case).

**N4 — Predicates and old worlds.** The describe pass's `is_done`
becomes "has description AND has additional_details", and revise mode
engages whenever an existing description is present (today it requires
the `rework` flag) — so enriching an old node keeps its prose and adds
depth, never clobbers. Existing worlds therefore upgrade organically:
any describe run (enrichment panel Run, agent `run_pass`, play-time
trickle) backfills details non-destructively; no migration step. The
runtime split: `ensure_current_node_detailed`'s blocking await-on-
arrival keeps its current condition (no name / no description — the
"thin air" case is the only thing worth stalling a turn for), while the
idle trickle and explicit runs use the pass predicate, so
details-missing nodes fill in quietly in the background.

**N5 — Who sees what (the context-flow rules).**

| Consumer | Gets `additional_details`? |
|---|---|
| Storyteller `<current_location>` (node + current sub-location) | yes, in a marked block with the primer line |
| Reader/extractor call | **no** — it extracts movement; halving exposure is free |
| Enrichment *neighbor* excerpts | **no** — a public description must not cite a neighbor's hidden depth |
| Enrichment of the node itself (revise/review-repair) | yes |
| Known-locations judge | yes — LLM-facing, and "this place is secret/lost" now lives there |
| Agent read tools / evaluator excerpts | yes — the builder sees everything |
| Any player-facing UI | **never** |

**N6 — Light up hidden connections.** Child-map connection generation
may emit `hidden: true` when the fiction warrants it (a schema field +
prompt line in the existing expansion call — the downstream runtime is
already built and verified); `add_connection` (v2a surgery) gains a
`hidden` argument. Same change audits the `include_hidden=True` call
sites (`wbruntime/schema.py` builds reader candidate options over
hidden connections' far nodes) so undiscovered ways don't pre-leak
through side channels.

**N7 — No redaction layer.** `world_data` ships to the client whole;
the UI simply never renders `additional_details`. Single-player
storytelling game — reading your own spoilers through devtools is a
choice, not a threat model. Recorded so nobody later "fixes" this into
a server-side filter without a new reason.

**N8 — Link tokens: same rules as description.** `additional_details`
goes through the same `postprocess_links` normalization and the same
rendering path, and the unresolved-link-token lint scans both fields.
One rule for node prose, no special cases. Length stays structural
("a few more sentences"), never a character cap (P9).

**Verified pass-through:** `save_node_enrichment` is field-generic
(`entry[field] = value`, both storage homes) and compiled nodes flow as
whole dicts — persistence and compiler need zero changes. The field is
added to the `MapNode` dataclass / `to_dict` (omitted when empty, like
`region`) and `edit_node` gains the argument (same write path).

## Deliberately unchanged

- `label_description` — stays the short public blurb.
- Connections keep one public `description`; `hidden` covers
  whole-object secrecy. Per-connection details are v2.
- Regions, maps, layers, lore — no details channel yet (v2).
- No UI affordance hinting that hidden details exist.

## Verification

Unit: describe JSON parse + fallback behavior (short/malformed output,
label fallback still description-only), revise-mode engagement without
`rework`, predicate split (arrival-blocking vs trickle), `edit_node`
argument, site bundle fields, hidden-connection authoring + surgery
argument, lint coverage of both prose fields. The pass/panel/SSE
surfaces are untouched (same passes, same events).

Live (Arc-B pattern, one recorded run): build a small world and check
(a) every described node carries details and the two fields read as
surface-vs-depth rather than duplicates, (b) arrival narration does not
dump `Secret:`-marked material, and a probing player action does earn
it, (c) the map UI shows only surface text, (d) at least one generated
hidden connection exists and the `discover_passage` flow still works.
Suite green module-by-path + root; `git checkout -- test_data` after.

## Sequencing (each lands alone, P8)

1. **Model + writers** — `MapNode.additional_details`, describe-call
   JSON output + mocks, site expansion fields, child-map + start-
   location authoring, `edit_node`, predicates/revise-mode (N4), link
   handling (N8). Size M.
2. **Runtime context flow** — location-block details + primer line,
   reader exclusion, neighbor-excerpt exclusion, known-locations
   inclusion, agent read tools (N5). Size S.
3. **Hidden connections** — generation schema + prompt, surgery
   argument, `include_hidden` audit (N6). Size S.

*Landed 2026-07-19 (87c9608, 505bbda, cca663a), with recorded
refinements against the sketch. Slice 1: ``generate_description``
returns a ``(description, additional_details)`` tuple (the
``generate_label`` precedent) with ``existing_details`` as a kwarg;
the N4 runtime split became two predicates —
``node_missing_essentials`` (arrival-blocking) vs ``node_needs_detail``
(trickle) — so old worlds enrich in the background but never stall a
turn; the interior generator's field whitelist would have silently
dropped the channel (fixed — the one writer the checklist missed);
the JSON-failure fallback yields description-only, leaving the node
pending on purpose (self-healing); ``add_node`` (surgery) gained the
field too — write parity, not just ``edit_node``; the authored root
and abstract-layer calls were covered as additional authoring
surfaces. Slice 2: the details render as one ``<storyteller_notes>``
block (node + site-level + current-sub entries behind a single
discipline header); the RAG question left open by N5 was decided
INCLUDE — all four lockstep entry builders (core index incl. legacy
branches, sync backfill, child maps, sites) append
`` Storyteller notes: ...`` so retrieval carries the marked depth.
Slice 3: the audited pre-leak was ``custom_transition_target``
offering a hidden way's far node ("beyond a known way") — now
visible-only; the player's map overlay filters hidden connections
(``discover_passage`` flips the flag in the session copy, so found
ways surface on their own); a hidden extra connection never counts as
the child map's anchor (the visible entrance is always inserted);
``connection_between``'s duplicate check was verified already
hidden-aware.*

## v2 candidates (recorded, unscheduled)

- A dedicated secrets/details pass if ride-along depth proves shallow
  in live use (the B1 registry makes it a file drop).
- Details channels for connections, regions, maps/layers.
- Discovered-state for details (the player's codex learning what they
  have earned; would need the state transition v1 deliberately skips).
- Within-site secret adjacency (a concealed door between sub-locations
  as *structure* rather than prose — today prose in
  `additional_details` carries it and the storyteller adjudicates).
