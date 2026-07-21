# Demo data seed

This directory is the seed for **demo mode** (`./start.sh --demo` /
`start.bat --demo`). On a demo launch the start script copies it to
`.demo_run/` (disposable, gitignored) and points the backend there via
`WB_DATA_DIR`, so the user's real `data/` — saves, characters, worlds,
scenarios, theme — is never read or shown. Every demo launch starts from a
pristine copy; changes made during a demo are thrown away on the next one.

API keys stay global: LLM provider configs (`data/providers/`) and the
image-gen config (`data/wb_image_gen/config.json`) are read from the real
data dir even in demo mode, so live generation works in demos without
copying secrets here. Never commit keys to this directory.

Layout mirrors `data/`:

- `prompt_library.json`, `global_prompt_pipeline.json`,
  `lorebooks/links.json`, `templates/players/default_player.wbp` — copies
  of the shipped app defaults.
- Showcase content goes in the same places it would live under `data/`:
  `saves/`, `characters/`, `worlds/`, `scenarios/`.
