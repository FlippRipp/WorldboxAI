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

Before the Storyteller generates narrative, a fast-model pre-assessment rates the player's action and injects an advisory `action_feasibility` block into the Storyteller prompt:

1. The pre-assessment sees the character sheet, world rules/lore, **and the last two storyteller outputs** — so NPC dispositions and established story facts inform the rating (e.g. a bold ask to a bored, receptive NPC is judged against the NPC's disposition, not the player's stats).
2. Feasibility is rated 1-10 against an explicit rubric: **1-2** = violates world rules or established facts (the only band where the attempt simply fails), **3-4** = far beyond current ability but not impossible, **5-6** = challenging, **7-8** = within demonstrated abilities, **9-10** = near-certain.
3. Creative, novel approaches that fit the established fiction rate one band higher than blunt attempts — ambition is rewarded, contradiction of established facts is punished.
4. The pre-assessment is a **referee, not a narrator**: it returns only the determination (feasibility score, difficulty, skill/curse/passive flags, and — for 1-2 only — a short factual failure reason). It writes no story prose.
5. The injected block states the ruling derived from the feasibility band (7-10 success, 3-6 partial success/success-at-a-cost, 1-2 failure) and **fails forward**: 3-6 is never a flat refusal, and even 1-2 failures tell the Storyteller to show why in world terms and how the world reacts.
6. The ruling decides only *whether* the action succeeds — how it plays out is the Storyteller's to narrate, adapting specifics to the living scene, and never resolving a non-impossible action as a dead end.

The `action_rating_strictness` slider (1-10) shifts judgment within the rubric: 1-3 is cinematic (favor the player, rule-of-cool), 4-6 balanced, 7-10 simulationist. Strictness never turns a merely unlikely action into a 1-2.

Only substantive actions are assessed: pure dialog with nothing at stake or trivial everyday actions (standing up, looking around, etc.) are skipped entirely — no feasibility ruling is generated or injected for that turn. Social *attempts* with a contested outcome (persuading, proposing, deceiving, intimidating) are substantive and are assessed.

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

The widget's "View Full Character Sheet" modal additionally supports **skill
editing**: each skill card has a pencil button opening an inline form (name,
type, rating, description, trigger words), plus delete and an "+ Add Skill"
button. Edits are saved immediately through the module API below and persist
into the active save.

## Skill Editing API

The module owns a router mounted at `/api/modules/wb_core_rpg` (via
`get_router()` / `set_services()`), operating on the active session's
`module_data.wb_core_rpg.skills`:

| Endpoint | Description |
|---|---|
| `POST /api/modules/wb_core_rpg/skills` | Add a skill. Body: `{name, rating?, description?, trigger_words?, type?}`. Defaults: rating 3, type `active`. |
| `PUT /api/modules/wb_core_rpg/skills/{skill_name}` | Update any subset of fields; `name` renames the skill (practice counters follow). |
| `DELETE /api/modules/wb_core_rpg/skills/{skill_name}` | Remove the skill and its practice counter. |

Validation: rating 1-10, type one of `active`/`passive`/`curse`, names stored
lowercase (matching Reader/librarian conventions), rename collisions are
rejected with 409. Every successful call persists the state to the active save
at the current turn and returns the full updated `skills` map.

## Migration from `wb_core_combat`

This module fully replaces `wb_core_combat`. Key differences:
- HP is now derived from Constitution level rather than a standalone number
- `hp_change` mutations work identically; no migration needed for Reader behavior
- Combat-specific settings (lethality, enable_gore) are replaced by the RPG system settings
- Old saves with `wb_core_combat` module data will not carry over — a new game is recommended
