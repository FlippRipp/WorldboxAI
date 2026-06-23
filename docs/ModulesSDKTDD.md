WorldBox SDK: Technical Design Document

1. Overview & Philosophy

The WorldBox SDK provides a backend-first, event-driven framework for extending the WorldBox roleplay engine. Unlike pure frontend HTML-injection frameworks, WorldBox modules execute natively in the core Python engine via a LangGraph state machine.

This provides developers with immense processing power while strictly protecting the user's UI layout, prompt architecture, and .wbx save file integrity.

2. Module Anatomy & File Structure

A WorldBox module must be completely self-contained within its own directory (e.g., /modules/core_combat/).

/core_combat/
├── manifest.json       # Required: Identity, dependencies, prompt blocks, and UI slots
├── backend.py          # Required: Python logic and LangGraph hooks
├── widget.vue          # Optional: Frontend UI component
└── /__assets__/        # Optional: Local media (images, audio)


The manifest.json Schema

Defines how the engine initializes the module, configures the UI, and registers Prompt Blocks.

{
  "id": "wb_core_combat",
  "name": "Core Combat",
  "version": "1.2.0",
  "dependencies": ["wb_core_stats"],
  "ui_slots": ["slot_sidebar"],
  "commands": { "/attack": "backend.py:force_attack" },
  "settings_schema": {
    "lethality": {"type": "slider", "min": 1, "max": 10, "default": 5}
  },
  "prompt_blocks": [
    {
      "id": "block_combat_state",
      "name": "Combat State JSON",
      "description": "Injects the player's current HP and active status effects."
    }
  ]
}


3. The LangGraph Execution Pipeline

Modules hook into the core Engine DAG via strictly defined function signatures in backend.py. The engine passes a state dictionary and a safe worldbox_sdk (SDK) object to these functions.

A. Context Gathering & Pre-Computation (on_gather_context)

Runs before prompt assembly. Used to do heavy math, database queries, or pre-compute state changes before the LLM needs to see them.

async def on_gather_context(state: dict, sdk: WorldBoxSDK) -> dict:
    # Recalculate passive health regeneration before the prompt is built
    current_hp = state["module_data"]["wb_core_combat"]["hp"]
    return {"module_data": {"wb_core_combat": {"hp": current_hp + 1}}}


B. Prompt Block Rendering (on_render_block_[id])

If the user has dragged a module's registered Prompt Block into their active load order, the LangGraph ContextBuilder will look for a dynamically named function to get the text for that block.

async def on_render_block_combat_state(state: dict, sdk: WorldBoxSDK) -> str:
    """Returns the text payload for 'block_combat_state'."""
    hp = state["module_data"]["wb_core_combat"]["hp"]
    # Wrap in XML for the LLM
    return f"<combat_state>{{\"player_hp\": {hp}, \"bleeding\": false}}</combat_state>"


C. State Mutation (on_mutate_state)

Runs after the LLM generates the story. Used to parse tool calls or regex from the LLM output and update the .wbx save.

async def on_mutate_state(llm_output: str, state: dict, sdk: WorldBoxSDK) -> dict:
    if "takes damage" in llm_output.lower():
        current_hp = state["module_data"]["wb_core_combat"]["hp"]
        return {"module_data": {"wb_core_combat": {"hp": current_hp - 10}}}
    return {}


4. Advanced SDK Features (Deep Dive)

4.1 The Asset Pipeline (Static Media)

Mechanism: FastAPI StaticFiles Mounting
If a module folder contains an __assets__ directory, the engine's Boot phase dynamically mounts it.

Security: The SDK locks the mount point explicitly to that subdirectory, preventing path traversal (../) attacks.

Frontend Usage: The module's widget.vue can reference files using a predictable, sandboxed URL structure:
<img src="/assets/wb_core_combat/sword_icon.png" />

4.2 Pre-Router Command Interception

Mechanism: Bypassing the LLM State Graph
Commands defined in manifest.json are intercepted at Node 0 of the graph. If a match is found, the engine executes the Python hook and terminates the turn, saving API costs.

# In backend.py
async def force_attack(args: list[str], state: dict, sdk: WorldBoxSDK) -> dict:
    target = args[0] if args else "the air"
    
    # Push a system message to the chat UI instantly
    sdk.ui.push_chat_message(f"System: You swing wildly at {target}.")
    
    # Return END signal to abort the LLM routing phase
    return sdk.Signal.END_TURN


4.3 The Event Bus (Cross-Module Pub/Sub)

Mechanism: In-Memory Event Emitter
Modules decouple from hard dependencies by broadcasting and listening to events via the SDK.

# Initialization hook in a Vampire Module
def on_init(sdk: WorldBoxSDK):
    sdk.events.on("character_sleep", trigger_thirst)

# The callback function
async def trigger_thirst(payload: dict, state: dict, sdk: WorldBoxSDK):
    hours = payload.get("duration", 8)
    # Logic to increase vampire thirst based on hours slept...


Emitting (from a Time Module): sdk.events.emit("character_sleep", {"duration": 8})

4.4 Custom LLM Endpoints (Background AI)

Mechanism: SDK Wrapper for LiteLLM
Modules can request their own independent AI generations without touching the user's main story context. The SDK enforces rate limits and routes the request using the player's global API keys.

async def generate_tavern_rumor(sdk: WorldBoxSDK) -> str:
    response = await sdk.llm.generate(
        prompt="Write a 1-sentence rumor about a local dragon.",
        model_preference="fastest", # Engine decides if this is Llama-3, Haiku, etc.
    )
    return response.text


4.5 Output Guardrails (The Validation Veto)

Mechanism: LangGraph Conditional Edges & Retry Loops
To enforce strict mathematical mechanics, modules can inspect the LLM's output before it reaches the user. If the LLM hallucinates an impossible action, the module triggers a Veto.

async def on_validate_output(llm_output: str, state: dict, sdk: WorldBoxSDK):
    player_gold = state["module_data"]["economy"]["gold"]
    
    # If the LLM narrates buying a sword but the player is broke
    if "purchases the iron sword" in llm_output.lower() and player_gold < 100:
        
        # Trigger the LangGraph to loop back to the Storyteller LLM
        raise sdk.ValidationVeto(
            reason="The player only has 10 gold, which is not enough to buy the sword. Rewrite the response so the merchant rejects the player."
        )


Engine Handling: LangGraph catches the ValidationVeto. It appends the reason to the internal System Prompt (usually by auto-injecting an invisible Veto Block at Depth 0) and reruns the Heavy LLM.

Safeguard: The engine enforces a MAX_RETRIES = 2. If the LLM fails 3 times, the SDK intercepts and outputs a generic system failure message to prevent an infinite loop.

5. UI Extensibility & The CSS Sandbox

Modules cannot use absolute positioning (position: fixed, z-index: 9999). They must anchor to engine-defined slots to guarantee mobile responsiveness.

slot_sidebar: Vertical widgets (stacks on PC, hides in a swipe-up drawer on mobile).

slot_header: Tiny top-bar icons. Overflows into a menu on mobile.

slot_chat_feed: In-line UI injected into the narrative chat flow (e.g., 3D dice rolls).

slot_modal: Floating popup windows (auto-expands to full screen on mobile).

slot_tab: Dedicated, full-page interfaces away from the chat screen.

Auto-Settings: Developers define a settings_schema in the manifest. The engine auto-generates a uniform settings menu. Modules access user preferences via state["module_configs"]["module_id"].

6. The Security Model

WorldBox employs a three-tiered security strategy:

Open-Source Mandate: To be loaded via the standard mod manager, a module's code must be hosted on a public repository (e.g., GitHub) for community auditing.

The AST Inspector: Before execution, the engine parses the module's Python Abstract Syntax Tree (AST). It strictly blocks dangerous imports (os, sys, subprocess, requests). Modules communicate only through the sandboxed worldbox_sdk.

Absolute User Liability: Enabling a third-party module triggers a mandatory warning: "WARNING: You are enabling a third-party Python script. This code will execute on your device. Malicious modules can compromise your computer, steal data, or incur API costs. WorldBox takes NO responsibility for community-created code. ONLY proceed if you completely trust the creator. [I Understand and Accept the Risk]"