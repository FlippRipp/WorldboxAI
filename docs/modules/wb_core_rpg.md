# Core RPG System (`wb_core_rpg`)

The Core RPG System module provides character stats, skills, leveling, health, and action feasibility assessment for WorldBox. It replaces the earlier `wb_core_combat` module with a full RPG mechanics layer.

## Overview

| Feature | Description |
|---|---|
| **Stats** | Six standard attributes: Strength, Dexterity, Constitution, Intelligence, Wisdom, Charisma. Default 10 each. |
| **Skills** | Open-ended, AI-driven skill names with a 1-10 rating. Skills are created dynamically by the AI as they become relevant to the story. |
| **HP** | Health points derived from Constitution level. Formula: `(CON * hp_per_constitution) + (level * 2)`. Configurable via settings. |
| **Leveling** | Three progression systems selectable in settings: XP-Based, Practice-Based, Milestone-Based. |
| **Action Feasibility** | Before each turn, the module injects the character sheet and action context into the Storyteller prompt. The Storyteller assesses feasibility and narrates accordingly. |
| **Stat/Skill Improvement** | After each turn, the module processes mutations from the Reader agent to update HP, stats, skills, and XP. |
| **Slash Commands** | `/stats` — view character sheet; `/skills` — list skills with ratings; `/level` — view level and XP progress. |

## Data Model

Stored in `state["module_data"]["wb_core_rpg"]`:

```json
{
  "stats": {
    "strength": 12, "dexterity": 14, "constitution": 11,
    "intelligence": 10, "wisdom": 13, "charisma": 10
  },
  "skills": {
    "swordsmanship": {"rating": 3, "description": "Skilled with one-handed blades"},
    "stealth": {"rating": 5, "description": "Adept at moving unseen"}
  },
  "level": 1,
  "xp": 45,
  "hp": 85,
  "max_hp": 85,
  "practice_counters": {}
}
```

- Stats range from 1 to `max_stat_value` (configurable, default 20).
- Skills range from 1 to 10. New skills start at rating 3 by default.
- XP curve: `XP_needed(level) = floor(50 * level ^ steepness)` where steepness is configurable (1-5, default 2).

## Settings

| Setting | Type | Default | Description |
|---|---|---|---|
| `progression_system` | select | `xp` | How characters advance: XP-Based, Practice-Based, or Milestone-Based |
| `xp_curve_steepness` | slider 1-5 | 2 | Controls the exponential XP curve. Higher = slower leveling. |
| `hp_per_constitution` | slider 3-15 | 7 | Multiplier for HP calculation. CON 10 = 70 base HP at level 1. |
| `action_rating_strictness` | slider 1-10 | 5 | How harshly actions are judged. Higher = harder outcomes. |
| `skill_improvement_rate` | slider 1-10 | 5 | Controls how quickly skills improve in Practice mode. |
| `max_stat_value` | slider 15-30 | 20 | Maximum value any stat can reach. |
| `skill_pool_start` | slider 3-20 | 8 | Reserved for future skill point allocation on character creation. |

## Progression Systems

### XP-Based (default)
- Each action awards XP via the `xp_gained` mutation from the Reader agent.
- XP accumulates toward level thresholds.
- Level-up: +2 HP, +1 to two random stats (capped by `max_stat_value`).
- XP curve example (steepness=2): L1→L2: 50 XP, L2→L3: 200 XP, L3→L4: 450 XP.

### Practice-Based
- Each time a skill is used (detected via keyword matching in the action text), its practice counter increments by the skill's current rating.
- When the counter reaches `improvement_rate * current_rating`, the skill improves by 1.
- Skills improve naturally through repeated use.

### Milestone-Based
- Significant events (HP changes > 20, 3+ stat changes in one action) trigger level-ups.
- No XP tracking needed. DM/narrative milestones drive progression.

## Action Feasibility

Before the Storyteller generates narrative, the module injects an `<action_assessment>` block:

1. The character's current stats and skills are listed
2. The player's action text is presented
3. The Storyteller is instructed to silently assess feasibility (1-10 rating) based on the character's capabilities
4. The Storyteller narrates the outcome reflecting the rating — higher ratings mean easier success, lower ratings mean creative complications or failure
5. The Storyteller includes a `[Feasibility: X/10]` marker and extracts skill usage into `skill_changes`

This approach uses the existing Storyteller LLM call (no extra API cost) and keeps the assessment narrative-aware rather than mechanical.

## Pipeline Hooks

| Hook | Purpose |
|---|---|
| `on_gather_context` | Injects action feasibility prompt into context before Storyteller |
| `on_render_prompt_block` | Renders `character_sheet` (system) and `action_feasibility` (chat-depth-0) prompt blocks |
| `on_mutate_state` | Processes `hp_change`, `stat_changes`, `skill_changes`, `xp_gained` from Reader. Handles level-ups, skill practice, and milestone detection. |

## Slash Commands

| Command | Description |
|---|---|
| `/stats` | Displays current level, XP, HP, all six stats, and list of skills |
| `/skills` | Detailed skill list with rating bars and descriptions |
| `/level` | Shows current level, XP progress, and XP needed for next level |

## Sidebar Widget

Displays in `slot_sidebar`:
- **Level** badge
- **HP bar** (color-coded: green > 60%, yellow 30-60%, red < 30%)
- **Stat grid** (3x2 compact view)
- **Skills list** (top 5, with "more" indicator)

## Migration from `wb_core_combat`

This module fully replaces `wb_core_combat`. Key differences:
- HP is now derived from Constitution level rather than a standalone number
- `hp_change` mutations work identically; no migration needed for Reader behavior
- Combat-specific settings (lethality, enable_gore) are replaced by the RPG system settings
- Old saves with `wb_core_combat` module data will not carry over — a new game is recommended
