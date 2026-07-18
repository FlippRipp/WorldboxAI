# Location Coherence Plan

Places that clearly belong together ("the school" / "the school rooftop" /
"the storage building behind the school") must sit together on the map — at
world-generation time and when the story creates places on the fly.

## Problem

Map positions are procedural and typeless; every name is invented afterwards
by independent LLM calls that did not know where any other named place sat.
Two failure classes:

1. **Generation time**: labeling calls riffed on the same premise element
   ("a school") from opposite ends of the map — one node became the school,
   a distant one its rooftop.
2. **Play time**: places the story needs mid-scene were authored onto
   whatever unnamed map slot vaguely fit the region, ignoring both the place
   they belong to and the player's position.

## Landed

### M1 — Labeling containment rule (`f4d7684`)

A node is named as a standalone place; a name may present it as a part of
another location (rooftop, gate, storage, annex...) **only when that location
is in its neighbor list**. Both label prompts carry the rule; batch prompts
additionally warn that batch entries may be far apart and forbid deriving one
entry's name from another unless adjacent. Single-node calls now receive the
full list of names already on the map (they were blind to it); the batch
avoid-list no longer truncates to the last 40 names.

### M2 — Anchor-aware on-the-fly authoring (`f4d7684`)

`generate_start_location` / `author_location` offer each unnamed slot
annotated with its nearest named places (map-unit distances). With an anchor
node (the player's position, passed by travel's improvised-destination path)
slots sort closest-first and carry a player distance, and the prompt carries
placement rules: part-of / nearby requests go to the slot closest to the
named place, otherwise near the player. The start picker also treats "the
rooftop of the school" as matching the school instead of declaring no-match
and founding a duplicate.

### M3 — Sites grow sub-locations on demand (`7c17362`)

Legacy `site_maps` migrate into real interior child maps, so a place inside a
site is a node **on that site's own map** — placement correct by
construction. `MapExpansionEngine.grow` authors ONE new location for an
existing child map (name, type, description, adjacency; duplicate guard
returns the existing match instead of re-creating). The engine wires real
edges, positions the node one typical edge length beside its anchors,
persists to the child-map bundle, syncs session/save/RAG. The Reader gets a
`new_sub_location` field whenever the player is on a child map; travel grows
the map and moves the player there.

## Remaining

### M4 — Brand-new overworld nodes when no slot fits (task C)

The authoring call may answer `{"node_id": "NEW", "near_node_id": <existing
node>, ...}` instead of picking a slot: no offered position suits the place,
so found it directly beside the named node. Engine does the geometry (one
typical route leg from the anchor, angle clearest of existing nodes, one real
edge in); the LLM only decides *what* and *next to which place*.

**"No good slot" is a distance-tiered rule with real numbers in the prompt**
(the map's typical route leg — mean edge length — is stated, and slots
already carry numeric distances):

| Request kind | Good slot must be | Else |
| --- | --- | --- |
| Part-of ("the school's storage building") | within ~1 leg of the named place | NEW beside it |
| Nearby ("an inn by the docks", player-relative) | within ~2 legs | NEW |
| Region-level ("a cave in the Highlands") | any free slot in that region | NEW only if region has none |
| Unanchored ("a lonely lighthouse") | fit by nature (region/terrain/type) | NEW only if nothing fits |

Within the distance budget the best-*fitting* slot wins — closeness
qualifies, fit ranks. Persistence needs an append helper dispatching by map
(root → `map_generation` step data; child map → bundle, M3's path), plus the
usual session/save/RAG sync and compiled-cache invalidation. Invalid NEW
output falls back to today's best-existing behavior. New-node edges carry no
road geometry (plain link) — acceptable while NEW stays the escape hatch,
which the slot-first prompt ordering enforces.

### M5 — Interior-vs-adjacent boundary

Who decides whether a story-created place is a sub-location inside a site or
an adjacent node in the wider world:

- **The Reader decides first** (it knows the fiction) by which schema field
  it fills. Both field descriptions carry the discriminating rule:
  *could you walk there without leaving {place} / is it on its premises →
  inside; is it its own destination that happens to be close → adjacent.*
- **Offer `new_sub_location` on the overworld too**, whenever the current
  node is expandable — "a place inside {node name}". If the site has no
  interior map yet, fold the request into its first expansion (the expand
  call is told the interior must include this place).
- **Resolve anchors through map ancestry**: authoring an outside place while
  standing inside the school anchors at the school's overworld node, so
  "across the road" lands beside the school.
- **Cross-redirect escape hatches** so a wrong field choice self-corrects:
  the overworld author may answer `{"belongs_inside": "<named node>"}` → the
  engine grows that node's interior instead; `grow` may answer
  `{"belongs_outside": true}` → the engine falls through to the overworld
  path.

Decision cascade: Reader picks the field by the containment rule → the
authoring LLM may veto across the boundary → the engine owns placement
(slot vs NEW by the distance tiers, exact coordinates, edges, persistence).

## Order

M4 first (NEW nodes + distance tiers), then M5 (boundary work) on top — M5's
redirects need M4's overworld append path to exist.
