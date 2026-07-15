# WorldBox

Modular, AI-driven text-based roleplaying game engine.

## Quick Start

```powershell
# Install backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item backend\.env.example backend\.env
# Edit backend/.env with your GEMINI_API_KEY

# Install frontend
cd frontend
npm install

# Run both
.\start.bat
```

On Linux/macOS:

```bash
# Install backend
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp backend/.env.example backend/.env
# Edit backend/.env with your GEMINI_API_KEY

# Install frontend
cd frontend && npm install && cd ..

# Run both
./start.sh
```

Both start scripts pull the latest changes from git on launch (skipped gracefully if offline) and refresh pip/npm dependencies when an update was pulled.

## Image Generation

Story illustrations render through either the Novita AI cloud API or a local
A1111/Forge-compatible Stable Diffusion WebUI (started with `--api`), switched
in the Image Studio main-menu screen. `modules/wb_image_gen/image_server.bat`
/ `image_server.sh` set up and start the local server (clone SD WebUI Forge,
install its dependencies, launch with the right flags) in one command. See
[docs/SETUP.md](docs/SETUP.md#image-generation-optional).

## Documentation

See [docs/index.md](docs/index.md) for full documentation including:
- [Setup guide](docs/SETUP.md)
- [Task list & priorities](docs/TaskList.md)
- [Architecture & design](docs/WorldboxTDD.md)
- [Module contract](docs/MODULES.md)
- [Implementation plans](docs/TaskList.md#implementation-plans)

## Testing

```powershell
# Deterministic backend tests (no API key needed)
.\venv\Scripts\python.exe -m pytest

# Build frontend
cd frontend
npm run build
```
