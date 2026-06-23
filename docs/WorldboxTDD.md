WorldBox: Master Technical Design Document (TDD)

1. Executive Summary & Philosophy

WorldBox is a modular, AI-driven roleplay engine. It shifts the paradigm from frontend text-hacking (like SillyTavern) to a Backend-First Game Engine architecture. The core philosophy relies on strict separation of concerns: the Engine handles state orchestration, LLM API routing, and security, while sandboxed Python Modules handle specific game mechanics (e.g., combat, inventory, weather) via a standardized SDK.

Core Tech Stack:

Backend: Python 3.10+, FastAPI (with WebSockets for streaming).

Orchestration: LangGraph (State Graph Architecture).

AI Integration: LiteLLM (multi-provider support) + Multi-Agent routing.

Database: SQLite (Master state), ChromaDB/LanceDB (Vector Memory).

Frontend: React or Vue 3 (Vite + TailwindCSS) with a strict Layout Manager.

2. Data Structures & Save Architecture

WorldBox utilizes a "Template vs. Instance" architecture to protect core blueprints while allowing infinite, non-destructive playtime mutations.

2.1 Templates (The Blueprints)

.wbp (Player Template): Base character stats, appearance, and starting module states (e.g., base inventory).

.wbs (Scenario Template): The starting world state, prompt, and initial lore database.

2.2 The Instance (.wbx Save Archive)

Once a game starts, the engine clones the templates into a standalone .wbx (zip) archive.

/Characters/ & /Module_States/: Isolated JSON files for active modules. (e.g., core_combat.json).

Graceful Degradation: If a module is uninstalled, its JSON is ignored. The AI treats orphaned mechanical data as narrative flavor text.

Rolling Snapshots (Undo System): The engine saves the last 10 JSON state states in a /Snapshots/ folder. If a user hits "Undo", the engine reverts the JSON and commands the Vector DB to delete the latest embeddings, preventing state desync.

2.3 The Memory Layer (RAG)

Recency Bias: Every Vector DB entry is tagged with a turn_generated timestamp. In the event of conflicting semantic memories (e.g., Turn 10: "King is alive" vs Turn 50: "King is dead"), the engine forces the LLM to trust the highest timestamp. This safely handles Time Travel or retcons.

Memory Fading (Garbage Collection): The Librarian Agent assigns an "Importance Score" (1-10) to new memories. Low-importance details naturally decay and are automatically purged from the database after a set number of turns to prevent save file bloat.

3. The Execution Pipeline (LangGraph)

The turn loop uses a Multi-Agent State Graph to separate complex math from creative writing.

The Router (Optional Pre-Flight): Intercepts hardcoded slash-commands (/roll) to bypass the LLM and execute module logic instantly.

Context Gathering (DAG Tiers): The engine calculates module dependencies (Topological Sort) and runs all on_gather_context module hooks in parallel.

Prompt Assembly: The ContextBuilder formats the retrieved RAG lore and module JSONs into the Component Block pipeline.

The Storyteller (Heavy LLM): A large model (e.g., Claude 3.5, Llama 70B) streams narrative prose directly to the user's UI. It is given no instructions about JSON formatting to preserve creative quality.

The Reader Agent (Post-Story Parsing): The moment the Storyteller finishes, a tiny, fast model (e.g., GPT-4o-mini, Llama 8B) reads the generated prose and the active module schemas. It outputs a strict JSON block detailing state mutations (e.g., {"core_economy": {"action": "subtract", "amount": 50}}).

State Mutation & Veto: Modules receive the Reader Agent's JSON.

If a module detects a mathematical impossibility (e.g., spending gold the player doesn't have), it raises a ValidationVeto. The LangGraph loops back to the Storyteller LLM, injecting the veto reason, and forces a rewrite (Max Retries: 2).

Background Tasks: Parallel async threads fire (e.g., Summarizer Agent compressing chat history, Event Bus triggers).

4. Prompt Assembly (Component Blocks)

To solve the "Lost in the Middle" LLM context problem, WorldBox uses a drag-and-drop Component Block system (inspired by SillyTavern) wrapped in XML tags.

Block Types: Static Text (Jailbreaks/Rules), Engine Native (RAG memory fetching), and Module Injected (e.g., combat_state).

Dynamic Injection: Users can set blocks to System_Relative (absolute top) or Chat_Injection (Depth 0, Depth 2) to force the LLM to pay attention to specific module math right before answering.

Module Hooks: Modules expose their state to the prompt via the on_render_block_[id] function.

5. Module Extensibility & SDK

Modules are self-contained folders (manifest.json, backend.py, widget.vue, /__assets__/).

5.1 Security Model (The 3 Tiers)

Open-Source Mandate: Modules must be hosted on public Git repositories for community auditing.

The AST Inspector: The engine parses the module's Python Abstract Syntax Tree (AST) before loading. It strictly blocks imports like os, requests, and subprocess.

Absolute User Liability: Enabling a module triggers a stark warning that third-party code is executing locally, shifting responsibility to the user.

5.2 The WorldBox SDK (Advanced Features)

Because standard Python libraries are blocked, modules interface with the engine purely through the worldbox_sdk object passed to their functions:

Asset Pipeline: The backend dynamically mounts a module's /__assets__/ folder via FastAPI, allowing secure serving of local images/audio to the frontend (/assets/module_id/image.png).

The Event Bus: Decoupled Pub/Sub communication. Modules can listen for (sdk.events.on) or broadcast (sdk.events.emit) triggers to other modules.

Custom LLM Endpoints: Modules can request independent, background AI generations using sdk.llm.generate(), utilizing the engine's built-in token tracking and model routing.

Off-Chain Plugins: Modules can register manual UI buttons to trigger asynchronous background tasks (like generating an EPUB of the story) without interrupting the game loop.

6. UI & Frontend Architecture

The React/Vue frontend acts as a "Pegboard" using a strict Layout Manager to ensure modules never overlap or break mobile views.

The CSS Sandbox: Modules cannot use position: fixed or absolute positioning. Content that exceeds its bounds must scroll internally.

Responsive Slots:

slot_sidebar: Stacks vertically on PC; converts to a swipe-up bottom-sheet on mobile.

slot_header: Top-bar icons; automatically overflows into a dropdown menu on mobile.

slot_chat_feed: In-line narrative widgets (e.g., 3D dice rolls) that naturally scroll with the text.

slot_modal: Popups that auto-expand to 100vw/100vh on mobile devices.

slot_tab: Dedicated full-page screens (e.g., a Lore Wiki or World Map).

Auto-Settings: The engine parses the settings_schema in a module's manifest.json and automatically renders a unified settings UI for the player, saving preferences to module_configs.json.