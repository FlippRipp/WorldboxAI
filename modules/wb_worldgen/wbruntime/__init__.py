"""Play-time runtime for the wb_worldgen module.

``backend.py`` (the module entry point the core server loads) is a thin
adapter; the actual turn-time logic lives here, split by concern:

  * worldspace.py — map/node accessors, fog BFS, small state readers
  * travel.py     — journeys, route finding, the movement mutation flow
  * context.py    — <current_location> / intro context blocks
  * schema.py     — the dynamic movement mutation schema
  * backfill.py   — silent background node detailing (queue + worker)
  * expansion.py  — lazy site/interior expansion tasks
  * sync.py       — three-way sync: session state -> save file -> RAG index

Every function that needs module-level services takes ``host`` as its first
argument: the backend module object itself (exposing ``world_builder``,
``_services``, ``_backfill``, ``_site_tasks``). State stays on the backend
module so tests can load backend.py under private names and monkeypatch those
attributes per instance; runtime code always reads them through ``host`` at
call time, never captures them at import.
"""
