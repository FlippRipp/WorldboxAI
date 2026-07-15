# WorldBox Setup

This guide covers the current local development setup for the WorldBox prototype.

## Requirements

- Python 3.10+
- Node.js 20+
- npm
- A Gemini API key for live LLM and embedding calls

## Backend Setup

Create and activate a virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

Install backend dependencies:

```powershell
pip install -r requirements.txt
```

Create your backend environment file:

```powershell
Copy-Item backend\.env.example backend\.env
```

Then edit `backend/.env` and set `GEMINI_API_KEY`.

If the live Storyteller model returns temporary provider errors, such as Gemini `503 Service Unavailable`, the backend retries and then falls back to a non-stream completion before failing the turn. You can tune this with:

```text
LLM_PROVIDER_RETRY_ATTEMPTS=2
LLM_PROVIDER_RETRY_DELAY_SECONDS=1
STORYTELLER_FALLBACK_MODELS=
```

`STORYTELLER_FALLBACK_MODELS` is a comma-separated list of additional LiteLLM model names to try after `STORYTELLER_MODEL`.

For deterministic backend smoke tests without live provider calls, set:

```text
LLM_MODE=mock
```

## Image Generation (optional)

Story illustrations are provided by the `wb_image_gen` module and configured
entirely in the **Image Studio** main-menu screen (no env vars). Two providers
are supported, switched with the provider toggle on the Setup tab:

- **Novita AI (cloud)** — paste a [novita.ai](https://novita.ai) API key and
  pick one of the thousands of hosted checkpoints. No GPU needed.
- **Local Stable Diffusion** — point the Studio at any A1111-compatible WebUI
  (AUTOMATIC1111, SD WebUI Forge, reForge, SD.Next) started with the `--api`
  flag (default address `http://127.0.0.1:7860`). Generation is free and
  private; the model dropdown lists your installed checkpoints. Optionally set
  the WebUI's `models/Stable-diffusion` and `models/Lora` folders in the
  Studio to enable one-click installs from the built-in browsers: the Setup
  tab's model browser finds checkpoints on Civitai (search, base-model /
  category / sort filters, per-version installs), and the LoRAs tab's browser
  covers Civitai and Hugging Face LoRAs. When the WebUI runs on a **different
  machine**, leave the folder fields empty and set the install helper URL
  instead: the bundled `image_server` script also starts a tiny companion
  server (`helper_server.py`, port 7861) next to the WebUI that downloads
  models into its folders on command — with live progress bars in the Studio —
  and reports exact installed-model hashes back for the browsers' badges. Set
  `WB_HELPER_TOKEN` on the server and paste it into the Studio to require
  auth on the helper.

To set up a local server from scratch, run the module's bundled script from
the repo root:

```powershell
.\modules\wb_image_gen\image_server.bat        # Windows
./modules/wb_image_gen/image_server.sh         # Linux/macOS
```

It clones [SD WebUI Forge](https://github.com/lllyasviel/stable-diffusion-webui-forge)
into `image_server/` (updating it on later runs), lets the WebUI install its
own dependencies on first launch (several GB, one time), and starts it with
`--api` on `http://127.0.0.1:7860` — exactly what the Image Studio expects.
The script prints the server address plus the checkpoint/LoRA folder paths to
paste into the Studio's Setup tab. An install directory can be passed as the
first argument, and `WB_WEBUI_REPO` / `WB_WEBUI_PORT` / `WEBUI_EXTRA_ARGS`
environment variables select a different A1111-compatible fork, port, or
extra launch flags (e.g. `--api-auth user:pass`, or
`--skip-torch-cuda-test --use-cpu all` on machines without an NVIDIA GPU).
The server starts without opening a browser tab (the WebUI's own interface
stays reachable at the printed address) and accepts connections from other
devices on the same network — the script prints the LAN URL; set
`WB_WEBUI_LISTEN=0` to bind to `127.0.0.1` only.

The script also copies the module's `wb_prompt_batch.py` into the WebUI's
`scripts/` folder. With it installed, a multi-image generation ("Images per
generation" > 1) renders as one GPU batch of different prompts instead of
queueing single-image requests — substantially faster. For a WebUI you manage
yourself, copy `modules/wb_image_gen/wb_prompt_batch.py` into
`<webui>/scripts/` and restart it; the Studio's connection test shows whether
the script was detected. The Setup tab's "GPU batch size" setting (default 4)
caps how many images share one batch — lower it if renders fail with CUDA
out-of-memory. Without the script everything still works, just serially.

All the booru-tag checkpoint families the module recognizes (Pony,
Illustrious, NoobAI, Animagine) work on the default Forge install, which the
Studio pairs with tag-style prompts and the family's own quality tags
automatically. Note that NoobAI v-pred checkpoints need a WebUI with SDXL
v-prediction support — Forge handles them out of the box, classic
AUTOMATIC1111 does not, so keep the default `WB_WEBUI_REPO` if you plan to
use them.

Start the backend:

```powershell
python main.py
```

The backend listens on `http://127.0.0.1:8321` (set the `WB_PORT`
environment variable to change it; the default avoids port 8000, which
SillyTavern and other tools commonly occupy).

## Frontend Setup

Install frontend dependencies:

```powershell
cd frontend
npm install
```

Start the frontend:

```powershell
npm run dev
```

The frontend usually listens on `http://localhost:5173`.

## One-Click Local Startup

After dependencies are installed, run:

```powershell
.\start.bat
```

This starts the backend and frontend in separate terminal windows.

## Health Check

When the backend is running, open:

```text
http://127.0.0.1:8321/api/health
```

The health response reports:

- loaded modules
- configured LLM models
- whether required API keys are present
- active save-backed session status
- memory database status

## Save API

The backend exposes a basic local save API:

```text
GET  /api/session
GET  /api/saves
POST /api/saves
POST /api/saves/{save_id}/load
POST /api/saves/{save_id}/undo
GET  /api/session/module-configs
PUT  /api/session/module-configs
GET  /api/session/prompt-pipeline
PUT  /api/session/prompt-pipeline
POST /api/session/prompt-pipeline/preview
```

Create save request body:

```json
{"save_id": "new_save"}
```

Undo request body:

```json
{"target_turn": 3}
```

Module config update body:

```json
{"module_configs": {"wb_core_combat": {"lethality": 7}}}
```

The frontend sidebar includes basic controls for this lifecycle.

The frontend header includes a `Prompts` button that opens Prompt Studio for editing and previewing the active save's prompt pipeline.

## Current Stabilization Focus

The project is currently in Stabilization D LLM pipeline hardening work. See `StabilizationPlan.md` for the approved plan and missing systems.

The live WebSocket game now creates or loads a default local save at `data/saves/autosave`. Save list/create/load/undo APIs are connected to the backend and basic frontend controls.

Saved sessions store both the AI narrative history and the visible chat message stream. The visible stream is stored in `Core/chat_messages.json` and includes both user and AI messages.

Module manifests, dependency loading, and backend hook signatures are documented in `MODULES.md`.

`LLM_MODE=mock` runs deterministic Storyteller, Reader, and embedding behavior for local smoke tests without a Gemini API key. `LLM_MODE=live` remains the default for normal gameplay.

Live Storyteller calls have retry and fallback handling. If the provider still fails, the WebSocket returns a structured `type: "error"` message and the failed turn is not saved.

Prompt block compilation, save-backed prompt pipeline persistence, draft preview, and Prompt Studio are documented in `PROMPTS.md`.

## Useful Commands

Run the deterministic backend pytest suite:

```powershell
.\venv\Scripts\python.exe -m pytest
```

`pytest.ini` limits default collection to deterministic tests. The default suite covers prompt compilation, mock engine turns, module contracts, save/session lifecycle, LanceDB memory behavior, API endpoints, save undo, and structured WebSocket error payloads. Live/manual scripts such as `test_engine.py`, `test_litellm.py`, `test_gemini_models.py`, and `test_ws.py` are intentionally excluded from the default suite.

Run backend smoke test:

```powershell
.\venv\Scripts\python.exe test_engine.py
```

Run deterministic mock engine smoke test:

```powershell
.\venv\Scripts\python.exe test_engine_mock.py
```

Run save manager smoke test:

```powershell
.\venv\Scripts\python.exe test_save_manager.py
```

Run module contract smoke test:

```powershell
.\venv\Scripts\python.exe test_module_contract.py
```

Run prompt pipeline smoke test:

```powershell
.\venv\Scripts\python.exe test_prompt_pipeline.py
```

Build frontend:

```powershell
cd frontend
npm run build
```
