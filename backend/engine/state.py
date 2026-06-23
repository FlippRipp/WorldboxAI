from typing import TypedDict, Any, Optional

class WorldState(TypedDict):
    active_save_id: Optional[str] # Current save/session id
    input_text: Optional[str]    # The player's input command
    module_data: dict[str, Any]  # e.g., {"wb_core_rpg": {"hp": 100}}
    module_configs: dict[str, Any] # User settings for modules
    characters: dict[str, Any]    # Active character state loaded from save
    current_context: list[str]   # Gathered context strings before LLM turn
    history: list[str]           # History of storyteller outputs
    chat_messages: list[dict[str, str]] # UI chat stream with user and AI messages
    prompt_pipeline: list[dict[str, Any]] # Configurable prompt block array
    last_prompt_trace: list[dict[str, Any]] # Debug trace from the latest prompt compile
    turn: int                    # Current turn number
    veto_retries: int            # Count of rewrite attempts for current turn (0-3)
    veto_reason: Optional[str]   # Reason injected into Storyteller on rewrite
    needs_rewrite: bool          # Flag set by on_validate_output to trigger rewrite
    world_id: Optional[str]      # ID of the selected world for this story
    player_location_node_id: Optional[str]  # Current map node the player is at
    player_location_region: Optional[str]   # Current region name the player is in
    player_location_layer_id: Optional[str] # Current layer the player is on
    revealed_node_ids: list[str]            # Node IDs revealed to the player (fog of war)
    world_data: Optional[dict[str, Any]]    # Cached compiled world data (rules, lore, regions, map, layers)
