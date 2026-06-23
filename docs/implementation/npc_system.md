# NPC System Module (`wb_npc_system`) — Implementation Plan

## Overview

Two agents work together to generate and introduce NPCs:

| Agent | When | Model | Hook | Job |
|-------|------|-------|------|-----|
| **Introduction Agent** | Every turn, before Storyteller | Fastest | `on_gather_context` | Decides if scene needs a character, picks best fit from bank |
| **Generator Agent** | Every X turns, after Storyteller | Balanced | `on_librarian` | Analyzes story direction, generates new NPCs to fill bank |

Both run parallelized via `_run_modules_in_levels` — no dependency on `wb_core_rpg` so they execute concurrently with other modules.

---

## 1. Engine Change: Add `on_librarian` Module Hook

**File:** `backend/engine/graph.py` — `librarian_node` method (line ~829)

Currently the librarian only does engine-level memory work. Modules have no `on_librarian` hook. We need to add one.

**Change:** After the existing memory summarization block, dispatch `on_librarian` to all modules in parallel via `_run_modules_in_levels`:

```python
async def librarian_node(self, state: WorldState):
    await self._ensure_memory()
    
    turn = state.get("turn", 1)
    history = state.get("history", [])
    
    # --- Existing memory pruning + summarization code stays here ---
    self.memory.purge_decayed_memories(turn)
    # ... existing LLM summarization, embedding, importance scoring ...
    
    result = {}
    if memory_id:
        result["last_stored_memory_id"] = memory_id
    
    # --- NEW: Dispatch on_librarian to all modules in parallel ---
    accumulated = await self._run_modules_in_levels(
        "on_librarian",
        state,
        merge_module_data=True,
    )
    
    if accumulated.get("module_data"):
        result["module_data"] = self._deep_merge(
            result.get("module_data", {}),
            accumulated["module_data"],
        )
    
    return result
```

**Key points:**
- Uses existing `_run_modules_in_levels` — same parallelism as `gather_context_node` and `reader_node`
- `merge_module_data=True` means modules return `{"module_data": {"wb_npc_system": {...}}}` and it merges automatically
- The hook name is `"on_librarian"` — modules implement `async def on_librarian(state, sdk) -> dict | None`
- Modules with `"on_librarian"` in the `produces.module_data` field get their module_data merged

---

## 2. Module: `wb_npc_system`

### 2.1 File Structure

```
modules/wb_npc_system/
  manifest.json
  backend.py
```

No widget needed — purely backend.

---

### 2.2 `manifest.json`

```json
{
  "id": "wb_npc_system",
  "name": "NPC System",
  "version": "1.0.0",
  "dependencies": [],
  "consumes": {
    "state": ["input_text", "turn", "history", "player_location_node_id", "player_location_region"],
    "module_data": [],
    "module_configs": [],
    "world_data": true
  },
  "produces": {
    "module_data": true,
    "context_string": false,
    "messages": false
  },
  "settings_schema": {
    "generator_frequency": {
      "type": "slider",
      "min": 3,
      "max": 20,
      "default": 5,
      "label": "NPC Generation Frequency",
      "category": "NPC System",
      "description": "Generate new NPC concepts every N turns"
    },
    "introduction_enabled": {
      "type": "toggle",
      "default": true,
      "label": "Auto-Introduce NPCs",
      "category": "NPC System",
      "description": "Let the Introduction Agent decide when to introduce new characters"
    },
    "max_unintroduced_pool": {
      "type": "slider",
      "min": 3,
      "max": 15,
      "default": 6,
      "label": "Max Unintroduced Pool",
      "category": "NPC System",
      "description": "Maximum NPCs kept in the unintroduced pool before generation stops"
    }
  },
  "mutation_schema": {
    "npc_introductions": "array of objects: {npc_id: string, name: string, first_impression: string (one sentence describing how they met), notes: string (optional evolving observations)}"
  },
  "prompt_blocks": [
    {
      "id": "npc_introduction",
      "type": "module_prompt",
      "enabled": true,
      "role_type": "system",
      "placement": "system_relative",
      "depth": null,
      "config": {}
    }
  ]
}
```

**Key manifest decisions:**
- `dependencies: []` — no dep on `wb_core_rpg`, runs in parallel with all other modules
- `consumes.world_data: true` — needs world lore/regions for location-aware NPC generation
- `consumes.state` — needs minimal state: input_text, turn, history, player location
- `produces.module_data: true` — persists NPC bank in save state

---

### 2.3 NPC Data Model

```python
{
    "id": str,                 # unique: "npc_dwarf_smith" or "npc_a1b2c3"
    "name": str,               # "Orin Ironvein"
    "race": str,               # "Dwarf"
    "gender": str,             # "male" | "female" | "nonbinary"
    "appearance": str,         # 1-2 sentence physical description
    "archetype": str,          # "retired-warrior-turned-crafter"
    "pitch": str,              # 2-3 sentence character concept
    "personality": [str],      # ["gruff", "honorable", "secretly sentimental"]
    "role": str,               # "quest_giver" | "antagonist" | "ally" | "informant" | "rival" | "neutral" | "wildcard"
    "encounter_type": str,     # "location_bound" | "encounter"
    "location_node_id": str|null,   # only if location_bound
    "location_region": str|null,    # only if location_bound
    "introduced": bool,        # has the player met this NPC?
    "met_turn": int|null,      # turn number when introduced
    "status": str,             # "unintroduced" | "active" | "departed" | "deceased"
    "notes": str,              # evolving notes about role in story (set by introduction agent / mutations)
    "created_turn": int        # turn when generated
}
```

---

### 2.4 `backend.py` — Full Implementation

#### Module-level constants

```python
NPC_ROLES = ["quest_giver", "antagonist", "ally", "informant", "rival", "neutral", "wildcard"]
NPC_STATUSES = ["unintroduced", "active", "departed", "deceased"]
DEFAULT_GENERATOR_FREQUENCY = 5
DEFAULT_MAX_POOL = 6
```

#### Helper: NPC bank access

```python
def _get_bank(state: dict) -> dict[str, dict]:
    """Get the NPC bank dict from module_data."""
    return state.get("module_data", {}).get("wb_npc_system", {}).get("characters", {})

def _set_bank(updates: dict, npcs: dict[str, dict]) -> dict:
    """Build a module_data return value with updated NPC bank."""
    return {"module_data": {"wb_npc_system": {"characters": npcs}}}

def _filter_candidates(state: dict) -> list[dict]:
    """Get unintroduced NPCs that could appear in the current scene."""
    bank = _get_bank(state)
    node_id = state.get("player_location_node_id", "")
    region = state.get("player_location_region", "")
    
    candidates = []
    for npc_id, npc in bank.items():
        if npc.get("introduced"):
            continue
        if npc.get("status") != "unintroduced":
            continue
        
        if npc.get("encounter_type") == "encounter":
            candidates.append(npc)
        elif npc.get("encounter_type") == "location_bound":
            if npc.get("location_node_id") == node_id or npc.get("location_region") == region:
                candidates.append(npc)
    
    return candidates
```

#### Helper: Build scene summary for agents

```python
def _scene_summary(state: dict) -> str:
    """Build a compact scene description for LLM agents."""
    history = state.get("history", [])
    recent = history[-5:] if len(history) > 5 else history
    
    parts = []
    parts.append(f"Location: node={state.get('player_location_node_id', 'unknown')}, region={state.get('player_location_region', 'unknown')}")
    parts.append(f"Player action: {state.get('input_text', '(system/game start)')}")
    parts.append(f"Turn: {state.get('turn', 0)}")
    
    if recent:
        parts.append("Recent events:")
        for i, h in enumerate(recent):
            parts.append(f"  [{i+1}] {h[:300]}")
    
    # Active story threads (tracked in module_data)
    threads = state.get("module_data", {}).get("wb_npc_system", {}).get("story_threads", [])
    if threads:
        parts.append(f"Active story threads: {', '.join(threads)}")
    
    return "\n".join(parts)

def _bank_summary(bank: dict[str, dict], introduced_only: bool = False) -> str:
    """Build a summary of existing NPCs for LLM context."""
    lines = []
    for npc_id, npc in bank.items():
        if introduced_only and not npc.get("introduced"):
            continue
        status = "(introduced)" if npc.get("introduced") else "(unintroduced)"
        lines.append(
            f"  - [{npc_id}] {npc.get('name')} — {npc.get('archetype')} "
            f"({npc.get('role')}, {npc.get('encounter_type')}) {status}\n"
            f"    Pitch: {npc.get('pitch', '')}"
        )
    return "\n".join(lines) if lines else "  (no NPCs yet)"
```

---

#### 2.4.1 `on_gather_context` — Introduction Agent

Runs every turn with fastest model. Decides if scene calls for a new character.

```python
async def on_gather_context(state: dict, sdk) -> dict | None:
    config = state.get("module_configs", {}).get("wb_npc_system", {})
    
    if not config.get("introduction_enabled", True):
        return None
    
    bank = _get_bank(state)
    candidates = _filter_candidates(state)
    
    if not candidates:
        return None  # no one available to introduce
    
    scene = _scene_summary(state)
    
    # Build candidate list for LLM
    candidate_text = ""
    for i, npc in enumerate(candidates):
        candidate_text += (
            f"[{i}] ID: {npc['id']} | {npc['name']} ({npc['race']}, {npc['gender']})\n"
            f"    Archetype: {npc['archetype']}\n"
            f"    Role: {npc['role']}\n"
            f"    Pitch: {npc['pitch']}\n"
            f"    Personality: {', '.join(npc.get('personality', []))}\n"
            f"    Type: {npc['encounter_type']}\n\n"
        )
    
    prompt = f"""You are a narrative director. Given the current scene, decide if a new character should be introduced.

SCENE:
{scene}

AVAILABLE CHARACTERS (unintroduced, in this location):
{candidate_text}

RULES:
- Only introduce a character if the scene naturally calls for one (player enters a populated area, seeks information, encounters travelers, etc.)
- Do NOT introduce anyone if the player is alone in wilderness, mid-combat, or the scene is self-contained.
- If introducing, pick the character that best fits the scene's tone and needs.
- Prefer location-bound NPCs over encounter NPCs when at their specific location.

Respond with ONLY valid JSON:
{{"introduce": true/false, "npc_id": "id or null", "reason": "one sentence why/why not"}}"""

    try:
        result = await sdk.llm.generate(prompt, model_preference="fastest", max_tokens=150)
        result = result.strip()
        # Strip markdown code fences if present
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
            result = result.strip()
        
        import json
        decision = json.loads(result)
    except Exception as e:
        print(f"[NPC System] Introduction Agent failed: {e}")
        return None
    
    if not decision.get("introduce"):
        return None
    
    npc_id = decision.get("npc_id")
    if not npc_id or npc_id not in bank:
        return None
    
    # Store pending introduction in module_data
    npc = bank[npc_id]
    return _set_bank(
        {"pending_introduction": npc_id, "introduction_reason": decision.get("reason", "")},
        bank,
    )
```

---

#### 2.4.2 `on_librarian` — Generator Agent

Runs every X turns after Storyteller. Heavy creative model. Generates new NPCs.

```python
async def on_librarian(state: dict, sdk) -> dict | None:
    config = state.get("module_configs", {}).get("wb_npc_system", {})
    frequency = config.get("generator_frequency", DEFAULT_GENERATOR_FREQUENCY)
    max_pool = config.get("max_unintroduced_pool", DEFAULT_MAX_POOL)
    turn = state.get("turn", 0)
    
    if turn == 0 or turn % frequency != 0:
        return None
    
    bank = _get_bank(state)
    
    # Count unintroduced NPCs
    unintroduced_count = sum(
        1 for n in bank.values()
        if not n.get("introduced") and n.get("status") == "unintroduced"
    )
    
    if unintroduced_count >= max_pool:
        return None  # pool is full
    
    # Build generation prompt
    scene = _scene_summary(state)
    bank_text = _bank_summary(bank)
    
    # World context
    world_context = ""
    world_data = state.get("world_data", {})
    if world_data:
        regions = world_data.get("regions", {}).get("regions", [])
        if regions:
            world_context += "Regions:\n"
            for r in regions:
                world_context += f"  - {r.get('name', 'unknown')}: terrain={r.get('terrain', '?')}, climate={r.get('climate', '?')}\n"
            world_context += f"  Factions: {', '.join(world_data.get('regions', {}).get('factions', []))}\n"
        lore = world_data.get("lore", {})
        if lore:
            world_context += f"Premise: {lore.get('premise', '')[:300]}\n"
    
    # Determine current region for location-bound NPCs
    region = state.get("player_location_region", "unknown")
    node_id = state.get("player_location_node_id", "")
    
    needed = min(3, max_pool - unintroduced_count)
    
    prompt = f"""You are a character designer for a text-based RPG. Create {needed} new NPC concepts for the game.

WORLD CONTEXT:
{world_context}

CURRENT STORY STATE:
{scene}

EXISTING CHARACTERS (DO NOT duplicate or create similar concepts):
{bank_text}

INSTRUCTIONS:
1. Create {needed} characters that fill gaps NOT covered by existing NPCs.
2. Each character must have a DISTINCT archetype, personality, and role from all existing ones.
3. At least one should be location-bound to the current region: {region}
4. The rest can be encounter-type (can appear anywhere).
5. Characters should feel authentic to this world's genre, factions, and regions.
6. Pitches should be 2-3 sentences — a hook that suggests story potential.

Respond with ONLY valid JSON:
{{
  "npcs": [
    {{
      "name": "string",
      "race": "string",
      "gender": "male|female|nonbinary",
      "appearance": "1-2 sentence physical description",
      "archetype": "short archetype label",
      "pitch": "2-3 sentence character concept with story hook",
      "personality": ["trait1", "trait2", "trait3"],
      "role": "quest_giver|antagonist|ally|informant|rival|neutral|wildcard",
      "encounter_type": "location_bound|encounter",
      "location_node_id": "node_id or null",
      "location_region": "region name or null"
    }}
  ]
}}"""

    try:
        result = await sdk.llm.generate(prompt, model_preference="balanced", max_tokens=1000)
        result = result.strip()
        if result.startswith("```"):
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
            result = result.strip()
        
        import json
        parsed = json.loads(result)
    except Exception as e:
        print(f"[NPC System] Generator Agent failed: {e}")
        return None
    
    new_npcs = parsed.get("npcs", [])
    if not new_npcs:
        return None
    
    # Add new NPCs to bank
    import uuid
    for npc_data in new_npcs:
        npc_id = f"npc_{uuid.uuid4().hex[:8]}"
        bank[npc_id] = {
            "id": npc_id,
            "name": npc_data.get("name", "Unknown"),
            "race": npc_data.get("race", ""),
            "gender": npc_data.get("gender", ""),
            "appearance": npc_data.get("appearance", ""),
            "archetype": npc_data.get("archetype", ""),
            "pitch": npc_data.get("pitch", ""),
            "personality": npc_data.get("personality", []),
            "role": npc_data.get("role", "neutral"),
            "encounter_type": npc_data.get("encounter_type", "encounter"),
            "location_node_id": npc_data.get("location_node_id"),
            "location_region": npc_data.get("location_region"),
            "introduced": False,
            "met_turn": None,
            "status": "unintroduced",
            "notes": "",
            "created_turn": state.get("turn", 0),
        }
    
    print(f"[NPC System] Generated {len(new_npcs)} new NPCs (bank size: {len(bank)})")
    return _set_bank({}, bank)
```

---

#### 2.4.3 `on_render_prompt_block` — Storyteller Injection

Passes the pending NPC introduction to the Storyteller.

```python
async def on_render_prompt_block(block: dict, state: dict, sdk) -> dict | None:
    block_id = block.get("id", "")
    
    if block_id != "npc_introduction":
        return None
    
    bank = _get_bank(state)
    npc_data = state.get("module_data", {}).get("wb_npc_system", {})
    pending_id = npc_data.get("pending_introduction")
    reason = npc_data.get("introduction_reason", "")
    
    if not pending_id or pending_id not in bank:
        return None  # no pending intro — skip this prompt block
    
    npc = bank[pending_id]
    
    content = f"""<npc_introduction>
A new character should be introduced in this scene. Weave them naturally into the narrative.

Character to introduce:
  Name: {npc.get('name')}
  Race: {npc.get('race')}
  Gender: {npc.get('gender')}
  Appearance: {npc['appearance']}
  Archetype: {npc.get('archetype')}
  Personality: {', '.join(npc.get('personality', []))}
  Narrative Role: {npc.get('role')}
  Character Pitch: {npc.get('pitch')}

Why they should appear now: {reason}

How to introduce them:
- Make their entrance feel organic to the scene
- Show their personality through action and dialogue
- Don't dump their entire backstory — reveal character through interaction
- Their NPC ID for player actions is: {npc['id']}

IMPORTANT: Mention the character's name clearly in the narrative so the Reader can identify them.
</npc_introduction>"""

    return {"content": content}
```

---

#### 2.4.4 `on_mutate_state` — Process NPC Introductions

Marks NPCs as introduced when the Reader detects them in the story output.

```python
async def on_mutate_state(mutation: dict, state: dict, sdk) -> dict | None:
    introductions = mutation.get("npc_introductions")
    if not introductions:
        return None
    
    if not isinstance(introductions, list):
        introductions = [introductions] if isinstance(introductions, dict) else []
    
    bank = _get_bank(state)
    turn = state.get("turn", 0)
    updated = False
    
    for intro in introductions:
        if not isinstance(intro, dict):
            continue
        npc_id = intro.get("npc_id", "")
        if not npc_id or npc_id not in bank:
            continue
        
        npc = bank[npc_id]
        npc["introduced"] = True
        npc["met_turn"] = turn
        npc["status"] = "active"
        
        impression = intro.get("first_impression", "")
        if impression:
            npc["notes"] = impression
        
        updated = True
        print(f"[NPC System] {npc['name']} ({npc_id}) introduced at turn {turn}")
    
    if updated:
        # Clear pending introduction so it doesn't repeat
        return _set_bank(
            {"pending_introduction": None, "introduction_reason": None},
            bank,
        )
    
    return None
```

---

#### 2.4.5 `on_command_npcs` — Debug Command (Optional)

List all NPCs the player has met or that are in the area.

```python
async def on_command_npcs(args: list[str], state: dict, sdk) -> dict:
    bank = _get_bank(state)
    
    introduced = {k: v for k, v in bank.items() if v.get("introduced")}
    nearby = {k: v for k, v in bank.items() if not v.get("introduced") and v.get("status") == "unintroduced"}
    
    lines = ["[NPCs]"]
    
    if introduced:
        lines.append("--- Known Characters ---")
        for npc in introduced.values():
            lines.append(f"  {npc['name']} ({npc['archetype']}) — {npc.get('role')} [{npc.get('status')}]")
    
    if nearby:
        location_node = state.get("player_location_node_id", "")
        lines.append(f"\n--- Nearby ({len(nearby)} unintroduced) ---")
        for npc in nearby.values():
            loc = ""
            if npc.get("encounter_type") == "location_bound":
                loc = f" @ {npc.get('location_node_id', npc.get('location_region', '?'))}"
            lines.append(f"  {npc['name']} ({npc['archetype']}) — {npc.get('role')}{loc}")
    
    if not introduced and not nearby:
        lines.append("  No NPCs generated yet.")
    
    return {"message": "\n".join(lines), "signal": "end_turn"}
```

---

## 3. Story Thread Tracking (Optional Enhancement)

Track active story threads to give agents better context on what characters are needed.

Add to `on_mutate_state` and/or create a periodic update in `on_librarian`:

```python
async def _update_story_threads(state: dict, sdk) -> list[str]:
    """Optional: ask LLM to extract active story threads from recent history."""
    history = state.get("history", [])
    if not history:
        return []
    
    recent = "\n".join(history[-5:])
    prompt = f"""Extract 3-5 active story threads from this RPG narrative. These are ongoing plotlines, goals, or tensions.

Narrative:
{recent[:2000]}

Respond with ONLY a JSON array of short thread descriptions:
["thread 1", "thread 2", ...]"""

    try:
        result = await sdk.llm.generate(prompt, model_preference="fastest", max_tokens=150)
        import json
        result = result.strip()
        if result.startswith("```"):
            result = result.split("```")[1]
        return json.loads(result)
    except Exception:
        return []
```

Call this periodically in `on_librarian` and store in `module_data.wb_npc_system.story_threads`.

---

## 4. Summary of Changes

| File | Change |
|------|--------|
| `backend/engine/graph.py` | Add `on_librarian` dispatch in `librarian_node` (~10 lines) |
| `modules/wb_npc_system/manifest.json` | New file (~60 lines) |
| `modules/wb_npc_system/backend.py` | New file (~350 lines) |

**No other files touched.** No frontend changes, no save system changes (NPC bank auto-persists via module_data), no API changes.

---

## 5. Testing Considerations

- **Empty NPC bank:** First turn, no NPCs exist yet. Introduction Agent should return no action. Generator should create first batch.
- **Full pool:** When unintroduced count >= max, Generator should skip. Introduction Agent still runs.
- **Location matching:** Location-bound NPC should only appear in Introduction Agent candidates when at their node/region.
- **Re-introduction prevention:** Once `introduced=true`, NPC should not appear in Introduction Agent candidates.
- **Parallel execution:** Confirm `on_gather_context` for `wb_npc_system` and `wb_core_rpg` run concurrently (different dep levels not needed since deps are both empty — they're in the same level and use asyncio.gather).
- **Mutation extraction:** Reader should see `npc_introductions` in schema and extract introductions from storyteller output.
- **Persistence:** NPC bank survives save/load via normal module_data serialization.
- **Configurable:** Changing `generator_frequency` or `introduction_enabled` in settings takes effect next turn.
