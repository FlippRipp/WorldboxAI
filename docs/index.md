# WorldBox Documentation

WorldBox is a modular, AI-driven text-based roleplaying game (RPG) engine that separates creative storytelling (LLMs) from game mechanics (sandboxed Python modules).

## Quick Links

| Document | Description |
|---|---|
| [SETUP.md](./SETUP.md) | How to install, configure, and run WorldBox locally |
| [TaskList.md](./TaskList.md) | Comprehensive implementation task list with priorities |
| [StabilizationPlan.md](./StabilizationPlan.md) | Current stabilization status and remaining work |

## Architecture & Design

| Document | Description |
|---|---|
| [WorldboxTDD.md](./WorldboxTDD.md) | Master Technical Design Document — overall philosophy and architecture |
| [ai_coder_blueprint.md](./ai_coder_blueprint.md) | Architecture blueprint and AI coding guidelines |
| [SavesTTD.md](./SavesTTD.md) | Save system architecture — templates, instances, snapshots, memory layer |
| [ModulesSDKTDD.md](./ModulesSDKTDD.md) | SDK technical design — module anatomy, hooks, event bus, security model |

## Contracts & Reference

| Document | Description |
|---|---|
| [MODULES.md](./MODULES.md) | Module contract — manifest fields, backend hooks, state access |
| [PROMPTS.md](./PROMPTS.md) | Prompt pipeline contract — block types, compiler, Prompt Studio |

## Systems & Features

| Document | Description |
|---|---|
| [systems/world-building.md](./systems/world-building.md) | World building system — AI cascade generation, module-extendable, user-reinforced |

## Module Documentation

| Document | Description |
|---|---|
| [modules/wb_core_rpg.md](./modules/wb_core_rpg.md) | RPG module — stats, skills, progression, HP, death state |

## Implementation Plans

| Document | Description |
|---|---|
| [implementation/llm-pipeline-hardening.md](./implementation/llm-pipeline-hardening.md) | Stabilization D: LLM output contracts, model validation, retry loops |
| [implementation/phase7-sdk-features.md](./implementation/phase7-sdk-features.md) | Phase 7: Event Bus, slash commands, validation veto, AST inspector |
| [implementation/module-implementation.md](./implementation/module-implementation.md) | Completing placeholder modules (dice, inventory, weather) |
| [implementation/technical-debt.md](./implementation/technical-debt.md) | Technical debt and polish items |
| [implementation/multi-save-session.md](./implementation/multi-save-session.md) | Multi-save and multi-session architecture |
| [implementation/ui-system-overhaul.md](./implementation/ui-system-overhaul.md) | Frontend UI rearchitecture: decomposition, security, mobile, accessibility |
| [implementation/module-engine-decoupling.md](./implementation/module-engine-decoupling.md) | Module-engine separation -- hardcoded references and safe module removal |
| [implementation/server-architecture-refactor.md](./implementation/server-architecture-refactor.md) | Server file split, dependency injection, global state cleanup |
| [implementation/code-quality-review.md](./implementation/code-quality-review.md) | Code quality audit -- deprecations, error handling, query patterns |

## Project Status

- **Phases 1-6**: Functionally complete (LangGraph pipeline, FastAPI + WebSocket, AI integration, save system, React frontend, RAG memory)
- **Stabilization A-D**: Mostly complete; D (LLM hardening) has remaining items
- **Phase 7**: Not started — deferred until stabilization is complete

See [TaskList.md](./TaskList.md) for the detailed task breakdown and priority order.
