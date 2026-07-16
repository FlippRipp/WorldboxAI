# Core RPG System (`wb_core_rpg`)

The Core RPG System module provides character stats, skills, leveling, health, and action feasibility assessment for WorldBox. It replaces the earlier `wb_core_combat` module with a full RPG mechanics layer.

## Overview

| Feature | Description |
|---|---|
| **Stats** | Six attributes: Power, Agility, Vitality, Intelligence, Spirit, Charm. Default 10 each. |
| **Skills** | Open-ended, AI-driven skill names with a 1-10 rating. Skills are created dynamically by the AI as they become relevant to the story. |
| **HP** | Health points derived from Vitality and level. Formula: `(vitality * hp_per_vitality) + (level * 2)`. Configurable via settings. |
| **Leveling** | Three progression systems selectable in settings: XP-Based, Practice-Based, Milestone-Based. Level-ups bank attribute and skill points that the player spends in a level-up popup. |
| **Skill Evolution** | A skill that reaches rating 10 can evolve: the AI offers 4 short themes (a pure amplification of the skill as-is, plus 3 distinct new directions — all significant power-ups), the player picks one, and the skill becomes a renamed, more powerful Tier N+1 form. |
| **Action Feasibility** | Before each turn, the module injects the character sheet and action context into the Storyteller prompt. The Storyteller assesses feasibility and narrates accordingly. |
| **Stat/Skill Improvement** | After each turn, the module processes mutations from the Reader agent to update HP, stats, skills, and XP. |
| **Slash Commands** | `/stats` — view character sheet; `/skills` — list skills with ratings; `/level` — view level and XP progress. |

## Data Model

Stored in `state["module_data"]["wb_core_rpg"]`:

```json
{
  "stats": {
    "power": 12, "agility": 14, "vitality": 11,
    "intelligence": 10, "spirit": 13, "charm": 10
  },
  "skills": {
    "swordsmanship": {"rating": 3, "description": "Skilled with one-handed blades", "trigger_words": ["slash", "parry"], "type": "active"},
    "brutal bladework": {"rating": 5, "description": "…", "trigger_words": ["cleave"], "type": "active",
                          "tier": 2, "lineage": ["swordsmanship"], "evolution_theme": "Brutal"}
  },
  "level": 3,
  "xp": 245,
  "hp": 85,
  "max_hp": 85,
  "practice_counters": {},
  "unspent_attribute_points": 2,
  "unspent_skill_points": 1,
  "pending_evolutions": [{"skill": "brutal bladework", "options": null, "status": "pending"}],
  "level_up_history": [{"level": 3}],
  "status_effects": [
    {"name": "broken leg", "description": "Broken leg from the fall.", "kind": "bad", "severity": 5, "duration_turns": 3, "expires_at_minutes": null, "turns_active": 2},
    {"name": "blessed", "description": "Blessed by the hearth-goddess.", "kind": "good", "severity": 4, "duration_turns": null, "expires_at_minutes": 5460, "turns_active": 1}
  ]
}
```

- Stats range from 1 to `max_stat_value` (configurable, default 20).
- Skills range from 1 to 10. New skills start at rating 3 by default.
- Evolved skills carry a `tier` (≥ 2), a `lineage` of former names, and the `evolution_theme` chosen when they evolved. Tier 1 skills omit these fields.
- XP curve: `XP_needed(level) = floor(50 * level ^ steepness)` where steepness is configurable (1-5, default 2).

## Status Effects

Temporary conditions — good (buffs, blessings) or bad (injuries, poisons, mind
control) — with a ONE-sentence description ("Broken leg from the fall.",
"Brainwashed by the cult leader.") and a severity (1-10, how strong/impactful
the condition is). The Reader applies and removes them from story events via
the `status_effects_gained` / `status_effects_removed` mutation keys.

The character bears at most **3** concurrent effects (`MAX_STATUS_EFFECTS`).
Re-applying an active effect refreshes its duration/severity/description
without counting against the cap; at the cap, a new effect only lands if it is
stronger than the weakest active one, which it overrides — otherwise it is
rejected.

A bad, indefinite effect (no duration) with severity ≥ 7 that has lingered for
10+ turns (`turns_active`) **hardens into a curse**: it leaves the effect list
and becomes a `type: "curse"` skill whose rating equals its severity.

Skills and status effects are mutually aware: a gained effect whose name
matches an existing skill/curse is skipped; a newly added skill (Reader
`skill_changes` or a librarian external grant) supersedes a same-named effect;
the librarian's external-event prompt lists active effects and is told not to
report temporary conditions as skill changes; and the module's
`on_mutation_schema` hook feeds the Reader the live skill and effect lists so
it never reports one as the other.

Durations, at most one per effect:

- `duration_turns` — remaining player turns. Ticks down once per turn in
  `on_mutate_state`; the turn an effect is gained is not ticked, so a 1-turn
  effect sways exactly one player action.
- `expires_at_minutes` — an absolute point on `wb_time_tracker`'s in-world
  clock (`clock.total_minutes_elapsed`), resolved from the Reader's
  `duration_minutes` when the effect is gained. The effect expires once the
  clock passes it. Without the time module there is no clock, so a
  minutes-based effect simply lasts until story events remove it.
- Neither set — indefinite; only `status_effects_removed` (or manual state
  editing) ends it. The Reader is instructed to reserve this for effects with
  no natural expiry — permanent until cured/dispelled, or actively sustained
  by an NPC or the environment — and to estimate a duration in all other
  cases rather than leaving both fields null when unsure.

Effects feed the action feasibility judge (a `[bad]` effect lowers the
feasibility of actions it would plausibly impede, a `[good]` effect raises
actions it aids), appear in the storyteller's character sheet as "Current
afflictions"/"Current boons", in `/stats`, and in the sidebar widget and
character view panel with their remaining duration.

While the global cheat toggle (`cheats.enabled`) is on, each effect in the
sidebar widget and full character sheet gets a ✕ button that removes it
outright via `DELETE /api/modules/wb_core_rpg/status-effects/{name}`. The
gate is enforced server-side — with cheats off the endpoint returns 403.

## Settings

| Setting | Type | Default | Description |
|---|---|---|---|
| `hardcore_mode` | toggle | off | Permanently locks all Core RPG settings at their current values. See below. |
| `progression_system` | select | `xp` | How characters advance: XP-Based, Practice-Based, or Milestone-Based |
| `xp_gain_condition` | select | `successful_action` | What earns the player XP (XP-Based only). See below. |
| `xp_per_action` | slider 1-50 | 10 | Base XP granted when the XP gain condition is met, scaled by action difficulty. |
| `xp_curve_steepness` | slider 1-5 | 2 | Controls the exponential XP curve. Higher = slower leveling. |
| `hp_per_vitality` | slider 3-15 | 7 | Multiplier for HP calculation. Vitality 10 = 70 base HP at level 1. |
| `action_rating_strictness` | slider 1-10 | 5 | How harshly actions are judged. Higher = harder outcomes. |
| `skill_improvement_rate` | slider 1-10 | 5 | Controls how quickly skills improve in Practice mode. |
| `max_stat_value` | slider 15-30 | 20 | Maximum value any stat can reach. |
| `attribute_points_per_level` | slider 0-5 | 2 | Attribute points banked per level-up for the player to spend (XP/Milestone modes). |
| `skill_points_per_level` | slider 0-3 | 1 | Skill points banked per level-up (XP/Milestone modes). |
| `new_skill_cost` | slider 1-5 | 3 | Skill points to learn a brand-new skill from the level-up popup; it starts at a rating equal to this cost. |
| `evolution_ai_model` | select | `smartest` | Model preference used for skill evolution themes and evolved forms. |
| `external_skill_events_enabled` | toggle | on | Post-turn detection of skills granted/removed/altered by external forces. |

### Hardcore Mode

`hardcore_mode` is a one-way lock, flagged `locks_module_settings` in the
manifest. Enabling it opens an in-app confirmation dialog; confirming saves
all staged settings immediately (the lock freezes the values on screen, so
they persist together with it — no separate Save & Close needed) and greys
out every Core RPG control in the settings modal — the toggle itself
included — and disables the module's advanced-settings button. The lock is
enforced server-side: while the stored value is on,
`PUT /api/session/module-configs` rejects any change to the module's section
with 403 (an unchanged section still saves, so other modules' settings keep
working). While the global cheat toggle (`cheats.enabled`) is on, an
"Unlock settings (cheat)" button appears in the locked section and the server
allows the section to change again, including turning the lock off. Any
module can opt into the same behavior by flagging one of its toggles with
`locks_module_settings` (plus an optional `confirm` message for the popup).
Hardcore Mode also freezes the module's custom instructions (below).

## Custom Instructions (per scenario / per story)

Every creative directive in the module's LLM prompts is a customizable
**instruction slot** (see `docs/MODULES.md`, "Customizable Instruction
Slots"). The scenario editor, story creation screen, and story settings modal
render one field per slot: empty means the built-in default, and an
AI-rewrite button adapts the default text to a typed request. A scenario's
values seed stories created from it, where they stay editable per story;
reset restores the scenario's value (or the built-in default when the story
has no scenario).

| Slot | Customizes |
|---|---|
| `action_assessment` | The judging guidelines of the action feasibility call — what kinds of attempts succeed, and therefore what earns XP. The strictness tier, outcome bands, and JSON contract stay fixed. |
| `skill_categories` | What the 10 add-skill wizard categories should be like (always exactly 10). |
| `skill_options` | What the 5 skill proposals in a category or search should be like (always exactly 5, uniform base strength). |
| `skill_refine` | How a picked draft skill is finalized. Fate's rolled rarity ladder stays fixed. |
| `evolution_options` | What the 4 evolution paths should be like (always 4, pure path listed first). |
| `evolve` | How the evolved Tier N+1 form is designed. The chosen theme requirement stays fixed. |

Overrides are stored under the reserved
`module_configs["__module_instructions__"]["wb_core_rpg"]` key; the wizard
and evolution routes read it from session state, and `on_gather_context`
receives it as `state["module_instructions"]` for the assessment call.

## Progression Systems

### XP-Based (default)
- XP accumulates toward level thresholds.
- Level-up: full heal, +2 max HP per level, and **banked points** — `attribute_points_per_level` attribute points and `skill_points_per_level` skill points the player allocates in the level-up popup (multiple level-ups in one turn accumulate points). There is no automatic stat assignment.
- XP curve example (steepness=2): L1→L2: 50 XP, L2→L3: 200 XP, L3→L4: 450 XP.

**XP Gain Condition (`xp_gain_condition`)** defines what actually earns XP. The
first three options award XP automatically each turn from the module's own
action assessment (the same fast-model pre-assessment used for feasibility), so
XP no longer depends on the Reader agent choosing to emit an `xp_gained` value:

| Condition | Awards XP when… |
|---|---|
| `successful_action` (default) | The turn's action is assessed and does **not** resolve as a hard failure (feasibility ≥ 3). |
| `any_action` | Any substantive action is assessed, success or failure. |
| `challenging_action` | The action's difficulty is `hard` or `extreme`. |
| `reader` | The Reader agent emits an `xp_gained` mutation (legacy AI-driven behaviour). |
| `disabled` | Never — XP progression is effectively off. |

For the automatic conditions the amount is `xp_per_action` scaled by the
assessed difficulty: trivial ×0.25, easy ×0.5, moderate ×1.0, hard ×1.75,
extreme ×2.5, impossible ×0. Only substantive actions are assessed — pure dialog
and trivial moves grant no XP.

### Practice-Based
- Each time a skill is used (detected via keyword matching in the action text), its practice counter increments by the skill's current rating.
- When the counter reaches `improvement_rate * current_rating`, the skill improves by 1.
- Skills improve naturally through repeated use.

### Milestone-Based
- Significant events (HP changes > 20, 3+ stat changes in one action) trigger level-ups.
- No XP tracking needed. DM/narrative milestones drive progression.
- Level-ups bank attribute/skill points exactly like XP mode. (Practice mode has no levels, so it never grants points.)

## Skill Evolution

Skills hard-cap at rating 10 — tiers are the only way past it. When an
`active` or `passive` skill reaches 10 (via practice, Reader mutations,
librarian events, or skill-point spending), it is queued in
`pending_evolutions` and the sidebar widget opens the evolution flow.
Both AI calls below receive the world context plus the story's themes and
tags (`story_style`) and the last three story scenes — provided as plain
context with no directive, so the model decides what to draw from it:

1. A "preparing skill progression options" screen fires `POST
   /skills/{name}/evolution-options`, which asks the AI for **exactly 4
   short themes** (1-3 words each) with a one-clause summary. The first is
   always the **pure path** (`kind: "pure"`) — the skill amplified without
   changing direction; the other three (`kind: "divergent"`) take it in
   distinct new directions (e.g. *Brutal / Efficiency / Stealthy*). All
   four must be significant power-ups. Options are cached on the pending
   entry, so reopening never re-calls the AI, and generation is serialized
   per skill so overlapping requests share one AI call.
2. The player picks a theme; an evolve animation plays while `POST
   /skills/{name}/evolve` runs. The AI designs the evolved form — new
   evocative name, tighter description, trigger words — required to be a
   dramatic power-up over the old form. The pure path keeps the skill's
   identity and amplifies it; divergent paths embody their theme.
3. The skill is replaced: `tier + 1`, rating reset to 5, `lineage` extended
   with the old name, practice counters migrated. It can climb to 10 and
   evolve again, indefinitely.
4. The evolve call also queues a one-shot note in `recent_evolutions`. The
   next storyteller generation gets a "JUST HAPPENED — SKILL EVOLUTION"
   block in the character sheet (old name → new name, tier, path,
   description) asking it to acknowledge the transformation in the
   narration; the note is dropped after that single generation.

Curse-type skills never evolve — evolution is a player-steered reward,
which would invert a curse's role as an affliction (the librarian already
escalates curses narratively). "Decide later" (`DELETE
/skills/{name}/evolution`) marks the entry `deferred`: the popup stops
auto-opening, and an "Evolve" badge on the skill reopens it. Skill tiers
are rendered into the assessment and storyteller prompts (`[Tier N]` plus a
guideline that each tier is a major step up), so evolved skills are
mechanically stronger, not just renamed.

## Action Feasibility

Before the Storyteller generates narrative, a fast-model pre-assessment rates the player's action and injects an advisory `action_feasibility` block into the Storyteller prompt:

1. The pre-assessment sees the character sheet, world rules/lore, **and the last two storyteller outputs** — so NPC dispositions and established story facts inform the rating (e.g. a bold ask to a bored, receptive NPC is judged against the NPC's disposition, not the player's stats).
2. Feasibility is rated 1-10 against an explicit rubric: **1-2** = violates world rules or established facts, **3-4** = far beyond current ability but not impossible, **5-6** = challenging, **7-8** = within demonstrated abilities, **9-10** = near-certain.
3. Creative, novel approaches that fit the established fiction rate one band higher than blunt attempts — ambition is rewarded, contradiction of established facts is punished.
4. The pre-assessment is a **referee, not a narrator**: it returns only the determination (feasibility score, difficulty, skill/curse/passive flags, and — for scores in the failure band — a short factual failure reason). It writes no story prose.
5. The injected block states the ruling derived from the difficulty tier's outcome bands (see below) and **fails forward**: the partial band is never a flat refusal, and even outright failures tell the Storyteller to show why in world terms and how the world reacts. In the fails-and-worsens band the Storyteller is told to add a concrete extra cost, threat, or complication on top of the failure.
6. The ruling decides only *whether* the action succeeds — how it plays out is the Storyteller's to narrate, adapting specifics to the living scene, and never resolving a non-impossible action as a dead end.

The `action_rating_strictness` slider (1-10) maps each value to a named difficulty tier; the judge is prompted with only the chosen tier's label and guidance (never the 1-10 scale). Each tier also sets the outcome bands — the classic four DM outcomes: the attempt fails **and the situation worsens**, fails, succeeds **partially at a cost**, or succeeds — and the same thresholds drive the storyteller ruling and success-conditioned XP (both failure bands earn nothing):

| Tier | Fails + worsens | Fails | Partial success at a cost | Success |
|---|---|---|---|---|
| 1 Power Fantasy | — | 1 | 2-4 | 5-10 |
| 2 Cinematic | 1 | 2 | 3-5 | 6-10 |
| 3 Heroic | 1 | 2 | 3-5 | 6-10 |
| 4 Favorable | 1 | 2 | 3-6 | 7-10 |
| 5 Balanced | 1 | 2 | 3-6 | 7-10 |
| 6 Gritty | 1-2 | 3 | 4-6 | 7-10 |
| 7 Demanding | 1-2 | 3 | 4-6 | 7-10 |
| 8 Harsh | 1-2 | 3-4 | 5-7 | 8-10 |
| 9 Merciless | 1-3 | 4 | 5-7 | 8-10 |
| 10 Brutal | 1-5 | — | 6-8 | 9-10 |

Power Fantasy never worsens a failure; on Brutal every failure worsens the situation. Tiers 1-8 tell the judge never to rate a merely unlikely attempt as an outright failure; Merciless and Brutal drop that guardrail — at Brutal success is almost impossible and anything beyond proven ability fails and makes things worse.

Only substantive actions are assessed: pure dialog with nothing at stake or trivial everyday actions (standing up, looking around, etc.) are skipped entirely — no feasibility ruling is generated or injected for that turn. Social *attempts* with a contested outcome (persuading, proposing, deceiving, intimidating) are substantive and are assessed.

## Pipeline Hooks

| Hook | Purpose |
|---|---|
| `on_gather_context` | Injects action feasibility prompt into context before Storyteller |
| `on_render_prompt_block` | Renders `character_sheet` (system) and `action_feasibility` (chat-depth-0) prompt blocks |
| `on_mutate_state` | Processes `hp_change`, `stat_changes`, `skill_changes` from Reader. Awards XP per the `xp_gain_condition` (from the action assessment, or the Reader's `xp_gained` in `reader` mode). Handles level-ups, skill practice, and milestone detection. |

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
into the active save. Manual sheet editing is a cheat: the pencil/add buttons
only appear while the global cheat toggle (`cheats.enabled`) is on, and the
skill CRUD endpoints below return 403 without it. Earned changes (level-up
point spending, evolutions) are normal gameplay and stay ungated.

## Skill Editing API

The module owns a router mounted at `/api/modules/wb_core_rpg` (via
`get_router()` / `set_services()`), operating on the active session's
`module_data.wb_core_rpg.skills`:

| Endpoint | Description |
|---|---|
| `POST /api/modules/wb_core_rpg/skills` | Add a skill. Body: `{name, rating?, description?, trigger_words?, type?}`. Defaults: rating 3, type `active`. |
| `PUT /api/modules/wb_core_rpg/skills/{skill_name}` | Update any subset of fields; `name` renames the skill (practice counters and pending evolutions follow). |
| `DELETE /api/modules/wb_core_rpg/skills/{skill_name}` | Remove the skill and its practice counter. |
| `POST /api/modules/wb_core_rpg/levelup/spend` | Spend banked points. Body: `{stat_allocations?, skill_allocations?, new_skill?}`. Validates totals against unspent points, stat caps, and the rating-10 ceiling before applying anything; returns the full rpg dict. |
| `POST /api/modules/wb_core_rpg/skills/{skill_name}/evolution-options` | AI call returning `{skill, tier, options: [{theme, summary, kind} ×4]}` — first option is `kind: "pure"`, the rest `"divergent"`. 409 unless the skill is an evolvable type at rating 10. Options are cached. |
| `POST /api/modules/wb_core_rpg/skills/{skill_name}/evolve` | Body `{theme}` (must match an offered theme when options are cached). AI call that applies the Tier N+1 form; returns `{rpg, evolved: {old_name, new_name, tier, theme, description}}`. |
| `DELETE /api/modules/wb_core_rpg/skills/{skill_name}/evolution` | Defer the pending evolution (stops the auto-opening popup; the Evolve badge remains). |

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
