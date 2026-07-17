# World Hierarchy: Designer's Guide

*No code knowledge required. The technical counterpart is `docs/systems/hierarchy.md`.*

## The big idea

A world is no longer one big map. It is **maps within maps** — a solar system whose planets open into world maps, whose cities open into street maps, whose castles open into room-by-room interiors. Nothing is generated until the story needs it, and everything generated is kept forever.

There are only two building blocks:

- **A Map** — a set of locations (nodes) with paths between them, at one scale. Every map looks and works the same whether it shows star systems or cellar rooms.
- **A Connection** — a way to get from a specific location on one map to a specific location on another. A door. A cave mouth. A shuttle route. A portal.

Everything else is built from these two pieces.

## Levels: the scale vocabulary

Each world template declares an ordered list of **levels** — plain-text labels like `solar system → planet → region → city → interior`. These are *vocabulary, not code*: the AI reads the label and its guidance text and decides what it means for this world. A "planet" level in one template could be "realm" in another and "dream layer" in a third.

Rules of thumb:
- A location can open into a map of **any smaller scale**, not just the next one down. A space station on the solar-system map goes straight to an interior; the planet next to it gets a full planetary map.
- The smallest scale can **nest into itself** (a ship docked inside a station; a vault inside a castle).
- **Parallel maps** exist at the same scale, side by side — the classic D&D surface world and its underworld are two parallel maps joined by cave-mouth connections.

## How a world is born

1. The player writes a seed prompt and picks a template (or none — default fantasy).
2. The AI designs the world's rules and lore, then its **structure**: which parallel maps exist, and which specific places deserve fully mapped sub-areas *right now* because the seed makes them central (the story's starting planet, the villain's fortress). Everything else waits.
3. The top-level map is generated with its major locations named and described.
4. Play begins. The rest of the world builds itself silently in the background, closest-to-the-story first.

Cost consequence for designers: creating a world is fast, and depth appears exactly where play goes — a city the party never visits never costs a token.

## Exploration mechanics (during play)

**Moving on the current map.** The player travels between locations along the map's paths. Long distances take multiple turns of narrated travel; interiors are effectively instant room-to-room.

**Passages between maps.** Every visible connection at or near the player is a *passage* the storyteller can use: "through the iron gate", "down the rotten stair", "aboard the shuttle". Passages are either instant (a door) or a journey of N turns (a shuttle crossing, narrated in transit). If the passage's entrance is across town, the player travels there first, then passes through — automatically, from one decision.

**Entering unmapped places.** Any *named* location can be opened up into its own map — the storyteller (or the player, via the map screen) simply goes in, and the interior is generated on approach in a single richly-detailed pass. Journeys prefetch their destination so arrivals feel seamless. Importance decides only what gets prefetched *automatically*; nothing is ever refused.

**Requirements are fiction, not locks.** A connection can carry a requirement — "guards admit members only", "needs a light source", "your own ship". The engine never hard-blocks; the storyteller enforces requirements narratively, playing the obstacle instead of picking the passage. This keeps stories from ever stalling on a mechanical gate.

**Improvised ways through.** When the story creates a way that isn't on the map — blowing a hole in a wall, lockpicking a window, squeezing through a sewer grate — the storyteller declares it, and chooses what it leaves behind:
- *One-time*: no trace (the picked window re-latches).
- *Open passage*: a permanent new connection (the hole in the wall is there forever, visible on the map).
- *Conditional passage*: a permanent but gated connection ("the window can be pried open quietly").

**Secrets.** Maps can carry **hidden connections** — secret doors, smugglers' tunnels. The storyteller knows they exist (and never volunteers them); the player discovers them through play, and once found they become normal passages on the map. Improvising a way through where a secret already was *discovers* it rather than duplicating it.

**Teleportation and other magic movement.** Instant travel to any location the player has *visited* — on any map, at any depth — with no connection needed. One-time by default; if the fiction establishes a reusable link (a bound portal circle), it becomes a permanent connection. And if the player teleports to a place that doesn't exist yet, the world *makes room*: the destination is authored onto a fitting unexplored spot and becomes a permanent part of the world.

## What the player sees

**The story is the interface.** The storyteller always knows the full local picture — where the player is in the hierarchy, what adjoins them, every visible way in and out — and is instructed to surface it naturally: on arrival, when the player looks around, when they ask "where can I go?". It is forbidden from inventing geography that contradicts the map.

**The map screen is the reference.** It shows the current map under fog of war, a breadcrumb trail up the hierarchy (`Aerathis › Thornhold › The Broken Keep`), a switcher for parallel maps, and markers on locations that lead somewhere — a diamond for a passage, an explore action for enterable places. Browsing the map never moves the player; only the story does.

**Fog of war** reveals the world as it's explored, per map. Entering a new map lights up the arrival point and its surroundings.

## What a designer can author (templates)

A template is a single JSON file — no code:
- the **level vocabulary** (labels + guidance per level, which generator draws each level)
- the framing voice ("you are a world building AI for a hard sci-fi setting…")
- vocabulary for connections ("spaceport", "jump gate") and sub-locations ("decks, domes, installations")
- form tweaks per generation step, pinned values, map density defaults

Drop the file in `data/world_templates/` and it appears in the world creation wizard. The generator palette is also open-ended: world-scale terrain maps and room-scale interiors ship now; star-system and region generators are labeled slots waiting to be filled.

## Persistent-world guarantees

- Everything generated — maps, connections, discoveries, improvised passages, teleport-created places — is written into the world and inherited by every future save of it. Nothing is generated twice.
- Every generation call is one focused, full-attention request (never batched), so depth never costs quality.
- Old worlds and saves keep working: layers become parallel maps, city districts become interior maps, automatically.

## Current limits (deliberate, revisit later)

- New *parallel planes* can't appear mid-play (a story-invented Feywild needs to be in the world's structure at creation; locations and interiors CAN appear mid-play).
- Off-screen NPCs cross between maps abstractly (they don't obey passage requirements or journey durations).
- A world's level vocabulary is fixed at creation.
