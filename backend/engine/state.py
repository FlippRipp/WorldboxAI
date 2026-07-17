from typing import TypedDict, Any, Optional

class WorldState(TypedDict):
    active_save_id: Optional[str] # Current save/session id
    input_text: Optional[str]    # The player's input command
    last_input_text: Optional[str] # The just-completed turn's player input, kept for post-turn phases (librarian)
    module_data: dict[str, Any]  # e.g., {"wb_core_rpg": {"hp": 100}}
    module_configs: dict[str, Any] # User settings for modules
    characters: dict[str, Any]    # Active character state loaded from save
    current_context: list[str]   # Gathered context strings before LLM turn
    history: list[str]           # History of storyteller outputs
    chat_messages: list[dict[str, str]] # UI chat stream with user and AI messages
    prompt_pipeline: list[dict[str, Any]] # Configurable prompt block array
    last_prompt_trace: list[dict[str, Any]] # Debug trace from the latest prompt compile
    last_reasoning: Optional[str] # Model chain-of-thought for the latest storyteller output
    last_retrieved_memory_ids: list[str] # Memory rows retrieved for the last turn's context
    last_retrieved_world_ids: list[str]  # world_entries rows retrieved for the last turn's context
    last_context_query: Optional[str]    # Query text used for the last RAG retrieval
    sticky_world_entries: dict[str, int] # Sticky lorebook entries: source_id -> last turn they stay in context
    lore_depth_injections: list[dict[str, Any]] # Active '@ depth' lorebook entries for the prompt compiler: [{depth, text}]
    last_stored_memory_id: Optional[str] # Librarian's most recently stored memory
    last_model: Optional[str]    # Model that produced the latest storyteller output
    last_usage: Optional[dict[str, Any]] # Token usage of the latest storyteller call, when the provider reported it
    continue_prompt: Optional[str] # Instruction injected as the user turn on an empty ("continue") send
    turn: int                    # Current turn number
    veto_retries: int            # Count of rewrite attempts for current turn (0-3)
    veto_reason: Optional[str]   # Reason injected into Storyteller on rewrite
    needs_rewrite: bool          # Flag set by on_validate_output to trigger rewrite
    world_id: Optional[str]      # ID of the selected world for this story
    player_location_node_id: Optional[str]  # Current map node the player is at
    player_location_region: Optional[str]   # Current region name the player is in
    player_location_map_id: Optional[str]   # Current map (hierarchy scope) the player is on
    revealed_node_ids: list[str]            # Node IDs revealed to the player (fog of war)
    world_data: Optional[dict[str, Any]]    # Cached compiled world data (rules, lore, regions, map, layers)
    story_style: dict[str, str]             # Editable story direction ({themes, tags, pacing}), injected at depth 0 every turn
