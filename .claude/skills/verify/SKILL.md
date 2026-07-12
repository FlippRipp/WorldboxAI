---
name: verify
description: Build, launch, and drive the WorldboxAI app end-to-end to verify frontend/backend changes at the real surface (browser UI).
---

# Verifying WorldboxAI changes

## Build & launch

- Backend (FastAPI, port 8321): `python main.py` from the repo root
  (`pip install -r requirements.txt` first; if pytest/uvicorn crash with
  `pyo3_runtime.PanicException` from `cryptography`, run
  `pip install -U --ignore-installed cryptography` — the system package is broken).
- Frontend (Vite dev server, proxies /api and /ws to 8321):
  `cd frontend && npm ci && npm run dev -- --port 5173 --strictPort`.
- There is no production static mount — always verify against the Vite dev server.

## Driving the UI

- Use Playwright with the preinstalled Chromium:
  `chromium.launch({ executablePath: '/opt/pw-browsers/chromium', headless: true })`.
  `playwright-core` is available globally (`/opt/node22/lib`); `npm install playwright-core`
  in the scratchpad if module resolution fails.
- No API key is configured in this environment, so the first-launch onboarding
  wizard appears. Skip it with
  `ctx.addInitScript(() => localStorage.setItem('wb_onboarding_done', '1'))`.
- Anything needing LLM calls (new story, generate buttons) won't work without keys.
  To get into the game screen, seed a save instead:
  `cp test_data/saves/save1.wbx data/saves/` and either load it through the save
  select screen or set `localStorage.wb_ui_state = {"mode":"storyteller-game","saveId":"save1"}`
  and reload (exercises the auto-restore path).
- To simulate the Android PWA relaunch after process death: same browser context,
  close the page, open a new page at `/` (fresh sessionStorage, shared localStorage).

## Flows worth driving

- Main menu → each screen (Settings, Character, save select) and back.
- Mid-story relaunch → should land back in the story, not the menu (`wb_ui_state`).
- Composer draft (`wb_draft`) and new-character form draft (`wb_character_draft`)
  surviving a relaunch; both clear on send/save/back.

## Cleanup

- Remove seeded saves: `rm -rf data/saves/save1.wbx data/saves/save1 data/saves/active_save.json`
  (the `data/` tree is the app's live data dir; check `git status` — parts may be tracked).
- Running the backend against a loaded save writes `data/saves/`; running pytest
  mutates `test_data/` — restore with `git checkout -- test_data`.
