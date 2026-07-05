# WorldBox World Building System — Design Document

## Overview

A core world building system that takes a user prompt and generates a complete roleplay world through an expanding cascade of AI-generated stages. Each stage inherits from the previous, and the user can stop, edit, re-roll, or approve at every level. The system is module-extendable — modules register new world aspects, generation logic, and gameplay context hooks.

## Philosophy

- **AI generates, user steers**: The LLM does the heavy lifting, but the user has full editorial control at every stage.
- **Cascade, not one-shot**: Generation expands outward from high-level rules to granular details, with each stage locked before the next begins.
- **Module-extendable**: Modules register schema fields, generation logic, and context hooks. The core defines the cascade structure; modules fill in their domain.
- **Rules constrain everything**: A separate world rules prompt defines the "physics" (genre, tone, magic level, tech era, lethality) which constrains all subsequent generation.

---

## Architecture: The Expanding Cascade

```
                    ┌─────────────────────────┐
  User Prompt ——→  │     WORLD RULES          │
  "a fungi world"  │  genre, tone, magic, tech │
                    └──────────┬──────────────┘
                               │ [user: approve / edit / re-roll / add note]
                               ▼
                    ┌─────────────────────────┐
                    │     OVERARCHING LORE     │
                    │  creation myth, history, │
                    │  central conflict, eras  │
                    └──────────┬──────────────┘
                               │
                               ▼
                    ┌─────────────────────────┐
                    │   REGIONS & GEOGRAPHY    │
                    │  3-8 regions, terrain,   │
                    │  climate, landmarks      │
                    └──────────┬──────────────┘
                               │
                               ▼
                    ┌─────────────────────────┐
                    │   FACTIONS & POWERS      │
                    │  nations, guilds, cults, │
                    │  goals, rivalries, power │
                    └──────────┬──────────────┘
                               │
                               ▼
                    ┌─────────────────────────┐
                    │    KEY CHARACTERS         │
                    │  faction leaders, NPCs,  │
                    │  personalities, quests   │
                    └──────────┬──────────────┘
                               │
                               ▼
          World compiled → RAG-embedded → save created → game starts
```

---

## Stage 1: World Rules

**Purpose**: Define the "physics" of the world. These rules constrain all subsequent generation stages.

**Input**: User's world prompt + a separate rules-focused AI prompt.

**Output**:

```json
{
  "world_name": "The Mycelium Expanse",
  "genre": "post-apocalyptic fungal fantasy",
  "tone": "eerie, survival-horror, wonder",
  "tech_era": "rust-era scavenged technology",
  "magic_level": "moderate — spore-based abilities",
  "lethality": 7,
  "narrative_style": "exploration-driven, faction politics",
  "scope": "continental",
  "generation_rules": [
    "No traditional gods — only fungal hiveminds",
    "All technology is pre-fall salvage",
    "Spore magic always risks infection",
    "Sunlight is lethal to pure fungal beings"
  ],
  "module_rules": {
    "wb_core_rpg": {
      "stat_tiers": "standard",
      "max_level": 20,
      "hp_per_con": 3
    }
  }
}
```

**User actions at this stage**: approve, edit fields manually, re-roll with same prompt, add a supplementary note like "Make it darker", go back (N/A at stage 1).

---

## Stage 2: Overarching Lore

**Input**: User prompt + locked world rules.

**Output**:

```json
{
  "premise": "Three centuries after the Sporefall, humanity survives in scattered enclaves. The fungal hiveminds that consumed civilization have begun to... dream.",
  "creation_myth": "The Sporefall was not an invasion — it was an awakening. The fungi had always been here, beneath the soil, waiting for humanity to weaken itself enough...",
  "central_conflict": "The Verdant Mind — the largest hivemind — has begun communicating with enclave leaders through infected intermediaries. Some see salvation, others see a trap.",
  "major_historical_events": [
    {"era": "Pre-Fall", "event": "Human civilization peaks, global network"},
    {"era": "Sporefall", "event": "Fungal bloom consumes 90% of population in 72 hours"},
    {"era": "The Silence", "event": "100 years of isolation, no faction contact"},
    {"era": "Re-emergence", "event": "Enclaves rediscover each other, trade resumes"},
    {"era": "The Dreaming", "event": "Hiveminds begin sending dreams to humans — present day"}
  ],
  "mysteries": [
    "What caused the Sporefall?",
    "Are the hiveminds truly sentient?",
    "What lies beyond the Spore Wastes?"
  ]
}
```

---

## Stage 3: Regions & Geography

**Input**: World rules + lore. "Create 3-8 distinct regions for this world."

**Output**:

```json
{
  "regions": [
    {
      "id": "spore_wastes",
      "name": "The Spore Wastes",
      "type": "fungal desert",
      "climate": "humid, perpetual spore-fog",
      "description": "Endless plains of phosphorescent fungus where the Sporefall was densest. The ruins of old cities are barely visible under centuries of growth.",
      "landmarks": ["The Spine — a mountain of calcified fungal towers", "The Whispering Fields — spore clouds that echo voices"],
      "dangers": "spore storms, fungal beasts, hivemind territory",
      "resources": "rare spores, pre-fall salvage",
      "travel_difficulty": "extreme",
      "connected_regions": ["enclave_valley", "rust_coast"]
    },
    {
      "id": "enclave_valley",
      "name": "Enclave Valley",
      "type": "temperate valley",
      "climate": "mild, protected by mountains",
      "description": "A sheltered valley where three major human enclaves have survived. The mountains keep most spore storms at bay.",
      "landmarks": ["The Iron Bulwark — enclave capital", "Lake Clarity — largest clean water source"],
      "dangers": "faction raids, resource shortages",
      "resources": "farmland, fresh water, iron",
      "travel_difficulty": "low",
      "connected_regions": ["spore_wastes", "sunken_city"]
    }
  ],
  "region_count": 6
}
```

---

## Stage 4: Factions & Powers

**Input**: World rules + lore + regions. "Create factions that inhabit these regions."

**Output**:

```json
{
  "factions": [
    {
      "id": "iron_enclave",
      "name": "The Iron Enclave",
      "type": "survivor nation",
      "region_id": "enclave_valley",
      "description": "The largest human settlement, built inside a repurposed pre-fall bunker complex. Militaristic but fair.",
      "leader": {"name": "Commander Voss", "personality": "stern, pragmatic, haunted"},
      "goals": ["Secure clean water sources", "Expand territory into Spore Wastes"],
      "allies": ["farmers_collective"],
      "rivals": ["spore_cult"],
      "military_strength": "high",
      "economic_strength": "moderate",
      "secrets": ["Voss has been receiving dreams from the Verdant Mind"]
    },
    {
      "id": "spore_cult",
      "name": "Children of the Spore",
      "type": "religious cult",
      "region_id": "spore_wastes",
      "description": "Humans who believe the fungi are the next stage of evolution. They willingly infect themselves with controlled spore strains.",
      "leader": {"name": "Mother Mycelia", "personality": "serene, fanatical, genuinely loving"},
      "goals": ["Spread the 'gift' of fungal communion", "Protect the hiveminds"],
      "allies": [],
      "rivals": ["iron_enclave", "farmers_collective"],
      "military_strength": "low",
      "economic_strength": "low",
      "secrets": ["Mother Mycelia was once Commander Voss's wife"]
    }
  ],
  "faction_count": 5
}
```

---

## Stage 5: Key Characters

**Input**: All above. "Create key NPCs, fleshing out faction leaders and adding independent characters."

**Output**:

```json
{
  "characters": [
    {
      "id": "voss",
      "name": "Commander Elena Voss",
      "faction_id": "iron_enclave",
      "region_id": "enclave_valley",
      "role": "Military leader of the Iron Enclave",
      "personality": "Stern and pragmatic on the surface, privately haunted by dreams she doesn't understand. Fiercely protective of her people.",
      "appearance": "Late 40s, scarred face, military posture, always in worn tactical gear",
      "motivation": "Keep humanity alive without losing their humanity",
      "relationships": [
        {"character_id": "mother_mycelia", "type": "former spouse, now enemy", "detail": "Doesn't know Mother Mycelia's true identity"}
      ],
      "quest_hooks": [
        "Voss asks the player to investigate the source of her dreams",
        "She needs a scout for a dangerous expedition into the Spore Wastes"
      ]
    }
  ],
  "character_count": 8
}
```

---

## User Reinforcement Flow

At each cascade stage, the user can:

| Action | Effect |
|---|---|
| **Approve** | Lock current stage, cascade to next |
| **Edit field** | Manually change a value (rename a region, adjust faction power, rewrite lore text) |
| **Re-roll** | Regenerate this stage only, keeping all previous stages locked |
| **Add note** | User-typed directive injected into the next stage's generation prompt ("Make one region entirely underwater", "Add a faction of sentient machines") |
| **Go back** | Unlock a previous stage, discard everything below it, re-cascade |

This creates an iterative, steered generation experience — the AI writes the draft, the user sculpts.

---

## Module Extension System

Modules hook into the world building cascade at multiple points. All hooks are optional.

### Extension Points

| Hook | When Called | What Module Provides |
|---|---|---|
| `on_world_rules_schema` | Before world rules generation | Module adds fields to the world rules output schema. Returns a dict of field-name → {type, description, default}. |
| `on_world_rules_generate(seed_prompt, core_rules, sdk)` | After core rules generated | Module generates its section of world rules using the seed + core rules as context. Returns module-specific rules dict. |
| `on_region_generate(region, world_state, sdk)` | Per region during region generation | Module adds module-specific data to each region. E.g., magic module adds `ley_line_density`, economy module adds `trade_goods`. Returns partial region dict. |
| `on_faction_generate(faction, world_state, sdk)` | Per faction during faction generation | Module adds module-specific faction data. E.g., magic module adds `mage_count`, combat module adds `garrison_strength`. Returns partial faction dict. |
| `on_character_generate(character, world_state, sdk)` | Per character during character generation | Module adds module-specific character data. E.g., RPG module adds `level`, `class`. |
| `on_world_compiled(world_state, sdk)` | After all stages complete | Module validates its data, resolves cross-references, runs integrity checks. |
| `on_world_inject_context(action, location, world_state, sdk)` | During gameplay turns | Module returns context string for the Storyteller about relevant world data. E.g., "Current region's magic density is high — spells are empowered here." |

### Example: Magic Module Hook

```python
# modules/core_magic/backend.py

async def on_world_rules_schema(state, sdk):
    return {
        "magic_source": {"type": "text", "description": "Source of magic in this world", "default": "ambient"},
        "magic_density": {"type": "text", "description": "How common magic is", "default": "rare"},
        "available_schools": {"type": "list", "description": "Magic schools available", "default": []},
    }

async def on_region_generate(region, world_state, sdk):
    rules = world_state.get("world_rules", {}).get("module_rules", {}).get("wb_core_magic", {})
    magic_density = rules.get("magic_density", "rare")
    
    # AI call to determine ley line presence
    prompt = f"Region: {region['name']} ({region['climate']}). Magic density: {magic_density}. Does this region have ley lines?"
    result = await sdk.llm.generate(prompt, model_preference="fastest")
    
    return {
        "ley_line_density": "high" if "ley" in result.lower() else "low",
        "magical_creatures": [],
        "arcane_sites": [],
    }

async def on_faction_generate(faction, world_state, sdk):
    rules = world_state.get("world_rules", {}).get("module_rules", {}).get("wb_core_magic", {})
    if not rules.get("available_schools"):
        return {"mage_count": 0, "arcane_strength": "none"}
    
    # Determine if faction is magical
    prompt = f"Faction: {faction['name']} — {faction['description']}. Does this faction practice magic? Return yes/no."
    result = await sdk.llm.generate(prompt, model_preference="fastest")
    
    if "yes" in result.lower():
        return {"mage_count": "moderate", "arcane_strength": "moderate"}
    return {"mage_count": 0, "arcane_strength": "none"}
```

---

## Data Model (Save-Backed)

The generated world lives in the `.wbx` save under a `World/` directory:

```
data/saves/my_world/
├── Core/
│   └── metadata.json
├── Characters/
├── Module_States/
├── Snapshots/
└── World/
    ├── metadata.json          ← world generation version, timestamp, seed_prompt
    ├── world_rules.json       ← stage 1 output
    ├── lore.json              ← stage 2 output
    ├── regions.json           ← stage 3 output
    ├── factions.json          ← stage 4 output
    ├── characters.json        ← stage 5 output
    ├── module_data.json       ← per-module world data (magic systems, economies, etc.)
    └── vector_index/          ← LanceDB: all world entries embedded for RAG retrieval
```

### World Metadata

```json
{
  "world_name": "The Mycelium Expanse",
  "seed_prompt": "a post-apocalyptic Earth where fungi have evolved sentience",
  "generated_at": "2026-06-20T18:00:00Z",
  "generation_version": "1.0.0",
  "cascade_stage": "complete",
  "user_notes": ["Make it darker — survival is hard"],
  "active_modules": ["wb_core_rpg", "wb_core_magic"]
}
```

### Embedding Strategy

Each world entry gets embedded for RAG retrieval during gameplay:

| Content Type | Embedding Text | Metadata | Retrieval Trigger |
|---|---|---|---|
| Region | `{name}: {description} — Climate: {climate}. Landmarks: {landmarks}` | `{type: "region", region_id, turn_generated: 0}` | Player enters region, asks about geography |
| Faction | `{name}: {description} — Leader: {leader}. Goals: {goals}` | `{type: "faction", faction_id, region_id}` | Player encounters faction influence |
| Character | `{name}: {role} for {faction}. Personality: {personality}. Motivation: {motivation}` | `{type: "character", character_id, faction_id}` | Player meets or asks about NPC |
| Lore event | `{era}: {event}` | `{type: "lore", era}` | Player researches history |

---

## World → Gameplay Integration

During gameplay, the world system injects context into turns:

### 1. Startup Injection
When the game starts, the Storyteller receives the world premise and rules as fixed system messages:

```
<world_rules>
Genre: post-apocalyptic fungal fantasy
Tone: eerie, survival-horror, wonder
Tech Era: rust-era scavenged technology
Generation Rules:
- No traditional gods — only fungal hiveminds
- Spore magic always risks infection
- Sunlight is lethal to pure fungal beings
</world_rules>

<world_premise>
Three centuries after the Sporefall, humanity survives in scattered enclaves...
</world_premise>
```

### 2. Per-Turn Context Injection
During `gather_context_node`, the engine:
1. Searches world embeddings relevant to the player's current action and location
2. Surfaces the current region's data (climate, dangers, nearby factions)
3. Module hooks add module-specific world context

```
<region_context>
You are in the Spore Wastes — a fungal desert with perpetual spore-fog.
Dangers: spore storms, fungal beasts, hivemind territory
Nearby factions: Children of the Spore
</region_context>
```

### 3. Location Tracking
The world system tracks the player's current region via a `player_location` field. Moving between regions triggers context refreshes and potential encounters.

### 4. Gradual Travel
Movement between map nodes is gradual rather than instant. When the Reader detects the player set out toward a destination, `wb_worldgen` computes the shortest route over the edge graph (Dijkstra on edge `distance`) and stores a journey record in `module_data.wb_worldgen.travel`:

```json
{
  "route": ["n_12", "n_7", "n_31"],
  "leg_index": 0,
  "leg_progress": 12.5,
  "leg_distance": 38.2,
  "destination_node_id": "n_31",
  "destination_region": "The Spore Wastes"
}
```

Each turn the journey advances by `avg_edge_distance / world.travel_turns_per_edge` map units (setting under World Building; `0` restores classic instant teleports). Waypoints reached along the way update `player_location_node_id`, reveal fog-of-war, and set the region from the node. While en route, the `<current_location>` context block switches to an EN ROUTE variant telling the storyteller the player has not yet arrived, with a turns-remaining estimate. The Reader can pause a journey (`travel_interrupted`, e.g. camping or a fight) or redirect it mid-way (a new destination reroutes from the last reached node). Inter-layer moves, unreachable destinations, and instant mode keep the old teleport behavior. The in-game map overlay shows the player marker interpolated along the current edge.

---

## API Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/world/generate/stage/{stage_number}` | Generate one cascade stage. Body: `{stage_data: {...}, user_notes: "..."}` |
| `GET` | `/api/world/status` | Get current cascade progress, all generated data |
| `PUT` | `/api/world/stage/{stage_number}` | User-edited stage data (approve/edit) |
| `POST` | `/api/world/stage/{stage_number}/reroll` | Re-roll a stage with same inputs |
| `DELETE` | `/api/world/stage/{stage_number}` | Go back — discard this stage and all below |
| `GET` | `/api/world/context` | Get injectable context for the current player location |

---

## Frontend: World Builder Wizard

A multi-step wizard UI that guides the user through the cascade.

### Wizard Flow

```
┌─────────────────────────────────────────────────────┐
│  World Builder                                       │
│                                                       │
│  Step 1 of 5: World Rules                      [Next]│
│                                                       │
│  ┌─────────────────────────────────────────────────┐ │
│  │ World prompt:                                     │ │
│  │ [a post-apocalyptic Earth where fungi...       ] │ │
│  │                                                   │ │
│  │ [Generate Rules]                                  │ │
│  └─────────────────────────────────────────────────┘ │
│                                                       │
│  ┌─ Generated Rules ───────────────────────────────┐ │
│  │ Genre:     [post-apocalyptic fungal fantasy  ]   │ │
│  │ Tone:      [eerie, survival-horror         ]   │ │
│  │ Tech Era:  [rust-era                      ]   │ │
│  │ Magic:     [moderate — spore-based     ]         │ │
│  │ Lethality: [●●●●●●●○] 7                          │ │
│  │                                                  │ │
│  │ [Approve]  [Re-roll]  [Add Note...]              │ │
│  └──────────────────────────────────────────────────┘ │
│                                                       │
│  Stage history:                                       │
│  ● World Rules (editing)                              │
│  ○ Overarching Lore                                   │
│  ○ Regions & Geography                                │
│  ○ Factions & Powers                                  │
│  ○ Key Characters                                     │
└─────────────────────────────────────────────────────┘
```

### Key UX Elements
- **Stage progress indicator**: Shows completed/locked stages, current stage, upcoming stages
- **Inline editing**: Each generated field is an editable input. No separate "edit mode" — always editable
- **Re-roll button per field group**: Re-roll the entire stage, or just a subsection
- **Note input**: A small text area below generated content for directing the AI ("Add more fungal monster types")
- **History breadcrumbs**: Click any completed stage to jump back and re-cascade from there (with confirmation)
- **Preview pane**: Shows the generated world as formatted text/cards before finalizing

---

## Implementation Plan

### Phase 1: Core Cascade Engine

| Task | Details |
|---|---|
| **World generation orchestrator** | `backend/engine/world_builder.py` — `WorldBuilder` class that manages cascade state, stage generation, validation |
| **Stage generation** | Each stage is an LLM call with structured output (Pydantic schema). Rules injected as constraints. |
| **Cascade state management** | Track current stage, locked stages, user notes. Persist to `World/` directory in save workspace. |
| **Backend API endpoints** | `POST /api/world/generate/stage/{n}`, `GET /api/world/status`, `PUT /api/world/stage/{n}`, `POST /api/world/stage/{n}/reroll`, `DELETE /api/world/stage/{n}` |
| **World compilation** | After stage 5 approved, embed all entries in LanceDB, write to `World/` directory |
| **Integration with session** | `GameSessionManager` detects world data in save, injects into initial state |

### Phase 2: Module Extension Hooks

| Task | Details |
|---|---|
| **Hook registration** | `ModuleRegistry` scans modules for world-building hook methods |
| **Schema extension** | `on_world_rules_schema` — merge module fields into rules generation schema |
| **Per-stage hooks** | `on_region_generate`, `on_faction_generate`, `on_character_generate` — called during cascade |
| **World compiled hook** | `on_world_compiled` — finalization and cross-referencing |
| **Context injection hook** | `on_world_inject_context` — surface world data during gameplay |

### Phase 3: Gameplay Integration

| Task | Details |
|---|---|
| **World embeddings** | Embed all world entries into LanceDB under world namespace |
| **Context injection in gather_node** | Search world embeddings based on player action + location |
| **Player location tracking** | Add `player_location` to WorldState, update on region transition |
| **Region transition detection** | Reader LLM detects when player moves regions, triggers context refresh |
| **World prompt blocks** | `world_rules`, `region_context`, `faction_presence` — auto-generated prompt blocks |

### Phase 4: Frontend Wizard

| Task | Details |
|---|---|
| **WorldBuilder component** | Multi-step wizard with stage navigation |
| **Stage renderers** | Per-stage components: RulesForm, LoreEditor, RegionGrid, FactionList, CharacterCards |
| **Inline editing** | All generated fields are editable inputs |
| **Re-roll + approve flow** | Buttons for each action, API calls, optimistic UI updates |
| **Note input** | User directive text area, passed to next stage generation |
| **Preview + finalize** | Read-only world overview before committing to save |
| **Integration with App.jsx** | Replace autosave creation with world builder flow on new game |

---

## Scope Decisions (Open)

| Question | Options |
|---|---|
| **Phase 1 scope** | A) All 5 stages + compilation + embeddings, B) Rules + Lore + Regions only, C) Rules only as MVP |
| **Frontend depth** | A) Full multi-stage wizard with approve/edit/re-roll, B) Single-page form, generate all at once, C) Backend-only API, manual testing |
| **Module hooks** | A) Wire all extension hooks from Phase 1, B) Core-only in Phase 1, hooks in Phase 2 |
| **Integration** | A) World builder replaces session startup flow (new game = world builder), B) World builder is optional (new game = blank world or builder) |

---

## Open Questions

1. **World generation models** — Should world generation use the same `STORYTELLER_MODEL` or a separate `WORLD_BUILDER_MODEL`? A cheaper model for generation stages vs. the expensive model for narrative?

2. **Image generation** — Should regions/characters have AI-generated images (DALL-E/Stable Diffusion integration)?

3. **World sharing** — `.wbx` export/import already works. Should there be a community world browser later?

4. **Dynamic world evolution** — Should the world evolve during gameplay (factions conquer regions, characters die) or stay static after generation?
