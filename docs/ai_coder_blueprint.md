WorldBox: Master Architecture & AI Coding Blueprint
Project Overview
WorldBox is a modular, AI-driven roleplay engine. It utilizes a Multi-Agent LangGraph pipeline on a Python backend, serving a responsive web frontend. The core philosophy is strict separation of concerns: the core engine handles orchestration, LLM API calls, and save states, while isolated "Modules" (plugins) handle game mechanics via a strictly defined SDK.
Tech Stack
Backend Server: Python 3.10+, FastAPI (with WebSockets for streaming text).
Orchestration: LangGraph (State Graph architecture).
AI Integration: LiteLLM (for multi-provider support).
Database (Memory): SQLite (Master text), ChromaDB or LanceDB (Local Vector Embeddings).
Frontend: React or Vue 3 (Vite + TailwindCSS).
Core Directory Structure (Target)
/worldbox
├── /backend
│   ├── /engine         # LangGraph pipeline, Event Bus, State Management
│   ├── /api            # FastAPI routes, WebSocket endpoints
│   ├── /sdk            # The worldbox_sdk exposed to modules
│   └── /memory         # RAG, SQLite, and Vector DB managers
├── /frontend
│   ├── /components     # UI Slots, Settings generators, Chat feed
│   └── /lib            # WebSocket clients, API services
├── /modules            # The dynamic drag-and-drop community modules
│   ├── /core_combat
│   └── /core_inventory
└── /saves              # .wbx archive files (JSON + DBs)


Architectural Golden Rules (For AI Assistant)
Never mutate state directly in the Storyteller LLM: The storytelling LLM only outputs prose. State mutations are handled by a secondary "Reader Agent" outputting JSON.
Modules are Sandboxed: Modules (backend.py) must only interact with the engine via the WorldBoxSDK object passed to them. They cannot import os, requests, or raw DB connections.
Template vs. Instance: A loaded game (.wbx) is a zipped snapshot. Modules read/write to isolated JSON namespaces within this zip, not to a global SQL database.
UI is Slot-Based: Frontend modules cannot use absolute positioning. They must anchor to engine-defined slots (slot_sidebar, slot_header).
Implementation Phases (Follow Strictly)
Phase 1: The Naked Engine (LangGraph + SDK)
Build the pipeline without LLMs first to ensure data flows correctly.
Create the WorldState TypedDict.
Build the ModuleRegistry that scans a /modules folder and loads manifest.json and backend.py files.
Implement the LangGraph execution loop: Context Gathering -> Storyteller (Mock text) -> State Mutation (Mock JSON parsing).
Phase 2: The Fast API Backend
Wrap the LangGraph engine in a FastAPI server.
Create a WebSocket endpoint that accepts { "action": "turn", "text": "I look around" }.
Stream the mocked LangGraph output back through the WebSocket.
Phase 3: The Multi-Agent AI integration
Connect LiteLLM.
Implement the "Storyteller" node (streams prose).
Implement the Post-Story "Reader Agent" node (takes the prose, looks at module schemas, outputs JSON state mutations).
Phase 4: Data Persistence (.wbx)
Write the logic to unzip a .wbx file into a temporary working directory.
Implement the "Rolling Snapshot" system (save JSON states every turn, keep last 10).
Phase 5: The Frontend Shell
Build the base React/Vue layout (Sidebar, Chat window).
Connect the WebSocket to render streaming text.
Build the dynamic WidgetLoader that reads active modules and renders their UI components into the correct slots.
Phase 6: RAG Memory
Implement the Librarian Agent (Entity extraction -> Semantic Hooks).
Connect ChromaDB and implement the timestamp turn_generated for Recency Bias.
