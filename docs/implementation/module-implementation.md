# Implementation Plan: Placeholder Module Completion

## Overview

Three modules (`core_dice`, `core_inventory`, `core_weather`) have valid manifests but empty/placeholder `backend.py` files. This plan covers the full implementation of each, turning them from UI-only shells into functional game mechanics.

---

## Module 1: Core Dice (`core_dice`)

### Current State

- `manifest.json`: Has `ui_slots: ["slot_chat_feed"]`, no mutation schema, no prompt blocks
- `backend.py`: Empty class with no hooks
- Frontend: No widget

### Goal

A complete dice-rolling module that responds to user dice commands, integrates with combat, and displays results in the chat feed.

### Implementation

#### Manifest Updates

```json
{
  "id": "wb_core_dice",
  "name": "Core Dice",
  "version": "1.0.0",
  "dependencies": [],
  "ui_slots": ["slot_chat_feed"],
  "settings_schema": {
    "default_dice": {"type": "slider", "min": 2, "max": 100, "default": 20},
    "auto_roll_on_combat": {"type": "toggle", "default": true},
    "show_animation": {"type": "toggle", "default": false}
  },
  "commands": {
    "/roll": "on_command_roll"
  },
  "mutation_schema": {
    "dice_roll": "string, dice roll notation (e.g., 2d6+3) that was performed. Empty string if no roll."
  },
  "prompt_blocks": [
    {
      "id": "dice_context",
      "type": "module_prompt",
      "enabled": true,
      "role_type": "system",
      "placement": "chat_injection",
      "depth": 1,
      "config": {}
    }
  ]
}
```

#### Backend Implementation

```python
# modules/core_dice/backend.py

import random
import re
from typing import Optional

class Backend:
    def __init__(self):
        self.last_roll = None

    async def on_gather_context(self, state: dict, sdk) -> Optional[dict]:
        config = state.get("module_configs", {}).get("wb_core_dice", {})
        default_dice = config.get("default_dice", 20)
        
        # Initialize module data if not present
        if "wb_core_dice" not in state.get("module_data", {}):
            return {"module_data": {"wb_core_dice": {"last_roll": None, "roll_history": []}}}
        return None

    async def on_command_roll(self, args: list[str], state: dict, sdk):
        config = state.get("module_configs", {}).get("wb_core_dice", {})
        notation = args[0] if args else f"d{config.get('default_dice', 20)}"
        
        result = self._parse_and_roll(notation)
        if result is None:
            return {"message": f"[Dice] Invalid notation: {notation}", "signal": "end_turn"}
        
        total, breakdown = result
        message = f"[Dice] {notation}: {total} ({breakdown})"
        
        return {
            "message": message,
            "signal": "end_turn",
            "state_update": {
                "module_data": {
                    "wb_core_dice": {
                        "last_roll": {"notation": notation, "total": total, "breakdown": breakdown}
                    }
                }
            }
        }

    async def on_mutate_state(self, mutation: dict, state: dict, sdk) -> Optional[dict]:
        dice_roll = mutation.get("dice_roll", "")
        if not dice_roll:
            return None
        
        config = state.get("module_configs", {}).get("wb_core_dice", {})
        if not config.get("auto_roll_on_combat", True):
            return None
        
        result = self._parse_and_roll(dice_roll)
        if result is None:
            return None
        
        total, breakdown = result
        
        # Push roll result to chat feed
        module_data = state.get("module_data", {}).get("wb_core_dice", {})
        roll_history = module_data.get("roll_history", [])
        roll_entry = {"turn": state["turn"], "notation": dice_roll, "total": total, "breakdown": breakdown}
        roll_history.append(roll_entry)
        
        return {
            "module_data": {
                "wb_core_dice": {
                    "last_roll": roll_entry,
                    "roll_history": roll_history[-20:]  # Keep last 20
                }
            }
        }

    async def on_render_prompt_block(self, block: dict, state: dict, sdk):
        module_data = state.get("module_data", {}).get("wb_core_dice", {})
        last_roll = module_data.get("last_roll")
        if not last_roll:
            return None
        
        return {"content": f"<dice>Last roll: {last_roll['notation']} = {last_roll['total']}. When the player attempts uncertain actions, incorporate this roll result into the narrative success/failure naturally.</dice>"}

    def _parse_and_roll(self, notation: str) -> Optional[tuple[int, str]]:
        """Parse dice notation like '2d6+3' and return (total, breakdown)."""
        match = re.match(r'^(\d+)?d(\d+)([+-]\d+)?$', notation.lower().strip())
        if not match:
            return None
        
        count = int(match.group(1) or 1)
        sides = int(match.group(2))
        modifier_str = match.group(3) or "+0"
        modifier = int(modifier_str)
        
        if count < 1 or count > 100:
            return None
        if sides not in [2, 4, 6, 8, 10, 12, 20, 100]:
            return None
        
        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls) + modifier
        
        if count == 1 and modifier == 0:
            breakdown = str(total)
        elif modifier == 0:
            breakdown = f"{'+'.join(map(str, rolls))} = {sum(rolls)}"
        else:
            mod_sign = "+" if modifier > 0 else ""
            breakdown = f"{'+'.join(map(str, rolls))}{mod_sign}{modifier} = {total}"
        
        return total, breakdown
```

#### Frontend Widget

File: `modules/core_dice/widget.jsx`

```jsx
// Chat-feed widget: shows latest dice roll inline
export default function DiceWidget({ state, config }) {
    const lastRoll = state?.module_data?.wb_core_dice?.last_roll;
    if (!lastRoll) return null;
    
    return (
        <div className="inline-flex items-center gap-2 px-3 py-1 bg-gray-700 rounded-lg text-sm">
            <span className="text-yellow-400">🎲</span>
            <span>{lastRoll.notation}</span>
            <span className="font-bold text-white">{lastRoll.total}</span>
        </div>
    );
}
```

#### Tests

File: `test_core_dice.py`

- Test: `/roll` with no args uses default dice
- Test: `/roll 2d6` returns correct range
- Test: `/roll d20+5` includes modifier
- Test: `/roll invalid` returns error message
- Test: `/roll 1000d6` rejected (too many dice)
- Test: `/roll d7` rejected (invalid sides)
- Test: auto_roll on combat mutation extracts dice_roll
- Test: roll_history capped at 20 entries
- Test: dice context appears in prompt when last_roll exists

---

## Module 2: Core Inventory (`core_inventory`)

### Current State

- `manifest.json`: Has `ui_slots: ["slot_header", "slot_modal"]`, no mutation schema, no prompt blocks
- `backend.py`: Empty placeholder
- Frontend: No widgets

### Goal

An inventory system where items are tracked, displayed in sidebar, managed via modal, and integrated into the Storyteller context.

### Implementation

#### Manifest

```json
{
  "id": "wb_core_inventory",
  "name": "Core Inventory",
  "version": "1.0.0",
  "dependencies": [],
  "ui_slots": ["slot_header", "slot_modal", "slot_sidebar"],
  "settings_schema": {
    "max_capacity": {"type": "slider", "min": 5, "max": 50, "default": 20},
    "weight_system": {"type": "toggle", "default": false},
    "show_descriptions": {"type": "toggle", "default": true}
  },
  "commands": {
    "/inventory": "on_command_inventory",
    "/inv": "on_command_inventory"
  },
  "mutation_schema": {
    "item_change": "object with 'action' (add, remove, use), 'item' (name string), and optional 'quantity' (integer). Null if no item change."
  },
  "prompt_blocks": [
    {
      "id": "inventory_context",
      "type": "module_prompt",
      "enabled": true,
      "role_type": "system",
      "placement": "chat_injection",
      "depth": 1,
      "config": {}
    }
  ]
}
```

#### Backend Implementation (Key Functions)

```python
class Backend:
    def __init__(self):
        self.default_items = []

    async def on_gather_context(self, state, sdk):
        # Initialize inventory if not present
        module_data = state.get("module_data", {}).get("wb_core_inventory")
        if module_data is None:
            return {
                "module_data": {
                    "wb_core_inventory": {
                        "items": [],
                        "capacity": 20
                    }
                }
            }
        return None

    async def on_mutate_state(self, mutation, state, sdk):
        item_change = mutation.get("item_change")
        if not item_change:
            return None
        
        action = item_change.get("action")
        item_name = item_change.get("item", "").strip()
        quantity = item_change.get("quantity", 1)
        
        if not action or not item_name:
            return None
        
        inventory = state.get("module_data", {}).get("wb_core_inventory", {})
        items = inventory.get("items", [])
        max_capacity = state.get("module_configs", {}).get("wb_core_inventory", {}).get("max_capacity", 20)
        
        if action == "add":
            # Check capacity
            if len(items) >= max_capacity:
                return None  # Full, LLM should be told in context
            
            existing = next((i for i in items if i["name"].lower() == item_name.lower()), None)
            if existing:
                existing["quantity"] = existing.get("quantity", 1) + quantity
            else:
                items.append({"name": item_name, "quantity": quantity, "acquired_turn": state["turn"]})
        
        elif action == "remove":
            existing = next((i for i in items if i["name"].lower() == item_name.lower()), None)
            if existing:
                existing["quantity"] = existing.get("quantity", 1) - quantity
                if existing["quantity"] <= 0:
                    items.remove(existing)
        
        elif action == "use":
            existing = next((i for i in items if i["name"].lower() == item_name.lower()), None)
            if existing:
                existing["quantity"] = existing.get("quantity", 1) - 1
                if existing["quantity"] <= 0:
                    items.remove(existing)
        
        return {"module_data": {"wb_core_inventory": {"items": items, "capacity": max_capacity}}}

    async def on_render_prompt_block(self, block, state, sdk):
        inventory = state.get("module_data", {}).get("wb_core_inventory", {})
        items = inventory.get("items", [])
        max_capacity = inventory.get("capacity", 20)
        
        if not items:
            return {"content": f"<inventory>The player's inventory is empty ({max_capacity} max capacity).</inventory>"}
        
        item_list = ", ".join(f"{i['name']} (x{i['quantity']})" for i in items)
        return {"content": f"<inventory>Player inventory ({len(items)}/{max_capacity}): {item_list}. The player can use, drop, pick up, or lose items based on the narrative.</inventory>"}

    async def on_command_inventory(self, args, state, sdk):
        inventory = state.get("module_data", {}).get("wb_core_inventory", {})
        items = inventory.get("items", [])
        max_capacity = inventory.get("capacity", 20)
        
        if not items:
            return {"message": f"[Inventory] Empty ({0}/{max_capacity} slots)", "signal": "end_turn"}
        
        lines = [f"[Inventory] ({len(items)}/{max_capacity} slots):"]
        for item in items:
            lines.append(f"  - {item['name']} x{item.get('quantity', 1)}")
        
        return {"message": "\n".join(lines), "signal": "end_turn"}
```

#### Frontend Widgets

- **Header widget**: Bag icon with item count badge, click opens modal
- **Sidebar widget**: Compact inventory list with item names + quantities
- **Modal widget**: Full inventory view with management (not editable during play — informational)

#### Tests

File: `test_core_inventory.py`

- Test: `/inventory` shows empty inventory
- Test: `/inventory` shows items after mutation
- Test: add item via mutation, verify it appears
- Test: remove item via mutation, verify it's gone
- Test: use item reduces quantity, removes at 0
- Test: capacity limit enforced
- Test: inventory context rendered in prompt block
- Test: duplicate item adds quantity instead of new entry

---

## Module 3: Core Weather (`core_weather`)

### Current State

- `manifest.json`: Has `ui_slots: ["slot_header"]`, settings schema present
- `backend.py`: Empty placeholder
- `widget.jsx`: Simple emoji button (displays an emoji but no real weather)

### Goal

A weather simulation with state machine (clear → rain → storm → clear), context injection, and a visual header widget.

### Implementation

#### Manifest

```json
{
  "id": "wb_core_weather",
  "name": "Core Weather",
  "version": "1.0.0",
  "dependencies": [],
  "ui_slots": ["slot_header"],
  "settings_schema": {
    "change_frequency": {"type": "slider", "min": 1, "max": 20, "default": 5},
    "track_seasons": {"type": "toggle", "default": false},
    "extreme_weather": {"type": "toggle", "default": false}
  },
  "commands": {
    "/weather": "on_command_weather"
  },
  "mutation_schema": {
    "weather_change": "string, the new weather condition if weather changed this turn. Null or empty if no change."
  },
  "prompt_blocks": [
    {
      "id": "weather_context",
      "type": "module_prompt",
      "enabled": true,
      "role_type": "system",
      "placement": "chat_injection",
      "depth": 0,
      "config": {}
    }
  ]
}
```

#### Backend Implementation

```python
class Backend:
    WEATHER_TYPES = ["clear", "cloudy", "rain", "storm", "fog", "snow"]
    
    def __init__(self):
        self._weather = None
        self._turns_since_change = 0
    
    async def on_gather_context(self, state, sdk):
        config = state.get("module_configs", {}).get("wb_core_weather", {})
        change_freq = config.get("change_frequency", 5)
        extreme = config.get("extreme_weather", False)
        
        module_data = state.get("module_data", {}).get("wb_core_weather", {})
        current = module_data.get("current", "clear")
        turns_since = module_data.get("turns_since_change", 0)
        
        # Advance weather state
        turns_since += 1
        new_weather = current
        
        if turns_since >= change_freq:
            new_weather = self._roll_new_weather(current, extreme)
            turns_since = 0
        
        weather_desc = self._describe(new_weather)
        
        return {
            "module_data": {
                "wb_core_weather": {
                    "current": new_weather,
                    "turns_since_change": turns_since,
                    "description": weather_desc
                }
            }
        }
    
    async def on_mutate_state(self, mutation, state, sdk):
        weather_change = mutation.get("weather_change", "")
        if not weather_change or weather_change not in self.WEATHER_TYPES:
            return None
        
        return {
            "module_data": {
                "wb_core_weather": {
                    "current": weather_change,
                    "turns_since_change": 0,
                    "description": self._describe(weather_change)
                }
            }
        }
    
    async def on_render_prompt_block(self, block, state, sdk):
        module_data = state.get("module_data", {}).get("wb_core_weather", {})
        current = module_data.get("current", "clear")
        description = module_data.get("description", "Clear skies")
        
        return {"content": f"<weather>The current weather is: {current} ({description}). Incorporate the weather into scene descriptions and its effects on visibility, movement, and comfort.</weather>"}
    
    async def on_command_weather(self, args, state, sdk):
        module_data = state.get("module_data", {}).get("wb_core_weather", {})
        current = module_data.get("current", "clear")
        description = module_data.get("description", "")
        emoji = self._weather_emoji(current)
        
        return {"message": f"{emoji} Weather: {current.capitalize()} — {description}", "signal": "end_turn"}
    
    def _roll_new_weather(self, current, extreme):
        transitions = {
            "clear": ["clear", "cloudy", "fog"],
            "cloudy": ["cloudy", "clear", "rain", "fog"],
            "rain": ["rain", "cloudy", "storm"],
            "storm": ["storm", "rain", "cloudy"],
            "fog": ["fog", "clear", "cloudy"],
            "snow": ["snow", "cloudy", "clear"],
        }
        if extreme:
            for key in transitions:
                if "storm" not in transitions[key]:
                    transitions[key].append("storm")
                if "snow" not in transitions[key] and key != "clear":
                    transitions[key].append("snow")
        
        options = transitions.get(current, ["clear"])
        return random.choice(options)
    
    def _describe(self, weather):
        descriptions = {
            "clear": "Clear skies, good visibility",
            "cloudy": "Overcast, dim light",
            "rain": "Steady rain, slippery ground",
            "storm": "Heavy storm, thunder and lightning, poor visibility",
            "fog": "Thick fog, severely limited visibility",
            "snow": "Snowfall, cold, tracks visible in snow",
        }
        return descriptions.get(weather, "")
    
    def _weather_emoji(self, weather):
        emojis = {
            "clear": "☀️", "cloudy": "☁️", "rain": "🌧️",
            "storm": "⛈️", "fog": "🌫️", "snow": "❄️"
        }
        return emojis.get(weather, "🌈")
```

#### Frontend Widget

Update `modules/core_weather/widget.jsx`:

```jsx
export default function WeatherWidget({ state, config }) {
    const weather = state?.module_data?.wb_core_weather;
    const current = weather?.current || "clear";
    
    const emojis = { clear: "☀️", cloudy: "☁️", rain: "🌧️", storm: "⛈️", fog: "🌫️", snow: "❄️" };
    const labels = { clear: "Clear", cloudy: "Cloudy", rain: "Rain", storm: "Storm", fog: "Fog", snow: "Snow" };
    
    return (
        <button
            className="flex items-center gap-1 px-2 py-1 rounded hover:bg-gray-600 text-sm"
            title={`${labels[current]} — ${weather?.description || ""}`}
        >
            <span>{emojis[current] || "🌈"}</span>
            <span className="hidden sm:inline">{labels[current]}</span>
        </button>
    );
}
```

#### Tests

File: `test_core_weather.py`

- Test: Initial weather is "clear"
- Test: Weather changes after `change_frequency` turns
- Test: `/weather` command shows current weather
- Test: `on_render_prompt_block` includes weather context
- Test: Transitions are sensible (no clear → storm directly)
- Test: Extreme weather setting allows storm from any state
- Test: Weather persists across turns
- Test: Manual weather_change mutation overrides simulation

---

## Shared Implementation Notes

### State Shape

All modules store persistent data under `state["module_data"][module_id]`:
```json
{
  "module_data": {
    "wb_core_dice": {"last_roll": null, "roll_history": []},
    "wb_core_inventory": {"items": [], "capacity": 20},
    "wb_core_weather": {"current": "clear", "turns_since_change": 0, "description": "Clear skies"}
  }
}
```

### Settings Access

All modules read user preferences from `state["module_configs"][module_id]`:
```json
{
  "module_configs": {
    "wb_core_dice": {"default_dice": 20, "auto_roll_on_combat": true},
    "wb_core_inventory": {"max_capacity": 20, "weight_system": false},
    "wb_core_weather": {"change_frequency": 5, "extreme_weather": false}
  }
}
```

### Testing Strategy

Each module gets its own test file using mock LLM mode:
1. Initialize state with module registered
2. Call hooks directly with test state
3. Verify state mutations are correct
4. Verify prompt rendering produces expected content
5. Verify command handlers return expected messages
