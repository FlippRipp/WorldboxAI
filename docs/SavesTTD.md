WorldBox Data Structures & Save Architecture

To ensure maximum flexibility, player evolution, and non-destructive module integration, WorldBox utilizes a strict "Template vs. Instance" architecture, paired with a resilient, self-cleaning memory layer.

1. The Templates (The Seeds)

These files are static starting points. They are never altered during gameplay, acting purely as blueprints.

A. Player Profiles (.wbp - WorldBox Player Template)

Purpose: A base character blueprint (e.g., "Garrick the Paladin").

Storage: Stored locally in a /templates/players/ directory.

Contents (JSON):

id, name.

module_data: Supports namespaced data. If a user rolls stats during character creation using a specific module, it is saved here to be imported into new games.

{
  "name": "Garrick",
  "module_data": {
    "wb_core_rpg": {"level": 1, "hp": 85, "max_hp": 85}
  }
}


B. The Scenario File (.wbs - WorldBox Scenario Template) [PLANNED, NOT IMPLEMENTED]

Purpose: The starting premise of a world before turn 1. Highly shareable.

Status: This format is documented for future implementation. No .wbs loading or saving code currently exists.

2. The Playthrough Save (.wbx - WorldBox Save)

Once a game starts, the engine copies data from the Templates into a .wbx file and cuts the cord. The Playthrough is a completely independent, living ecosystem.

Storage: A zipped archive containing the following structure:

/Core/

metadata.json: Playtime, current turn, world data (world_id, player_location_node_id, etc.).

chat_history.json: The complete, lightweight narrative history (AI story turns).

chat_messages.json: User and AI message pairs for the chat interface.

module_configs.json: Per-save module settings (editable via Settings modal).

prompt_pipeline.json: Per-save prompt pipeline configuration.

/Characters/ (The Living Players)

Supports multiple characters (e.g., player_garrick.json).

Character files inherit the module_data from their template, allowing modules to read/write safely as the game progresses (e.g., updating HP or tracking wounds) without altering the original .wbp file.

/Module_States/ (Graceful Degradation)

Isolated JSON files for active global modules.

Graceful Degradation: If a module is uninstalled mid-playthrough, the engine simply ignores its JSON file. If a character file has orphaned data from a deleted module, the engine ignores the mechanical stats.

/Snapshots/ (The Undo System)

To protect module data from "Undo/Reroll" actions, the engine keeps a rolling history of the last 10 turns.

Every turn, a zip of the Characters, Module_States, and Core files (chat_history.json, chat_messages.json, prompt_pipeline.json) is saved (e.g., turn_5.zip). Clicking Undo restores state from the target snapshot.

/World/ (World Data)

world_data.json: Generated world data from the world-building cascade.

3. The Memory Layer (Long-Term Lore)

The memory layer uses LanceDB for vector search, stored under the active save workspace (vector_index/ and world_index/ tables).

To prevent save file bloat and handle logical contradictions (like time travel or changing facts), the database enforces two core mechanics:

A. Recency Bias (Timestamping)

Mechanic: Every entry in the LanceDB table is tagged with a `turn_generated` metadata integer.

Function: Memory searches filter by `turn_generated <= current_turn`, excluding future-timestamped entries. Contradictory entries are both presented to the LLM (no automatic conflict resolution).

Time Travel/Rollback Support: If a player rewinds turns, `rollback_memories()` deletes entries where `turn_generated > target_turn`.

B. Memory Fading (Garbage Collection)

Mechanic: Vector DB entries are assigned an "Importance Score" (1-10) by the Librarian AI upon creation. `purge_decayed_memories()` in `memory.py` implements thresholds (importance <= 3: 10 turns; 4-7: 30 turns; 8+ or permanent: never expired).

Note: The purge function is implemented but not yet wired into any turn loop or scheduled job -- decayed entries are not automatically removed during gameplay.

Result: This mimics natural human memory, keeping the database lightning fast and preventing procedural or infinitely long playthroughs from bloating the save file to unmanageable sizes.