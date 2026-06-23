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
