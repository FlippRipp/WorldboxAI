# Implementation Plan: Multi-Save & Multi-Session Architecture

## Overview

WorldBox currently operates with a single-save, single-session model. The `GameSessionManager` manages one active save (`autosave`), and all game state is scoped to that single save. This plan covers the architectural changes needed for multiple concurrent saves and (optionally) multiple sessions.

---

## Current Architecture (Single-Session)

```
GameSessionManager
  └── active_save: "autosave"
       ├── Core/
       │   ├── metadata.json
       │   ├── chat_messages.json
       │   ├── prompt_pipeline.json
       │   └── module_configs.json
       ├── Characters/
       ├── Module_States/
       ├── Snapshots/
       └── vector_index/ (LanceDB)
```

### Current Limitations

1. **One active save at a time**: `GameSessionManager` holds a single `active_save_id`
2. **No save metadata browser**: Frontend lists save names but shows no details
3. **Live LanceDB not packed**: During gameplay, LanceDB writes to a workspace directory; `.wbx` packing is manual
4. **No session isolation**: All clients share the same state (single-user assumption)
5. **No save import/export**: `.wbx` files are on disk but there's no UI for management

---

## Phase 1: Multi-Save Management (Single Session)

### Goal

Support multiple saves with metadata, switching between them, and proper cleanup.

### 1.1 Save Metadata

Add per-save metadata tracking:

```python
# backend/engine/save_manager.py

class SaveMetadata:
    save_id: str
    created_at: datetime
    last_played: datetime
    total_turns: int
    total_playtime_seconds: float
    scenario_source: Optional[str]  # .wbs template used
    character_name: Optional[str]
    thumbnail: Optional[str]  # Base64-encoded or file path

class SaveManager:
    def __init__(self, saves_dir: Path):
        self.saves_dir = saves_dir
        self._metadata_cache: dict[str, SaveMetadata] = {}
    
    def get_all_saves(self) -> list[SaveMetadata]:
        """Scan saves directory and return metadata for all saves."""
        saves = []
        for save_dir in self.saves_dir.iterdir():
            if save_dir.is_dir():
                wbx = self.saves_dir / f"{save_dir.name}.wbx"
                if wbx.exists() or (save_dir / "Core" / "metadata.json").exists():
                    metadata = self._load_metadata(save_dir.name)
                    saves.append(metadata)
        return sorted(saves, key=lambda s: s.last_played, reverse=True)
    
    def _load_metadata(self, save_id: str) -> SaveMetadata:
        workspace = self.saves_dir / save_id
        core_metadata = workspace / "Core" / "metadata.json"
        
        if core_metadata.exists():
            with open(core_metadata, "r") as f:
                data = json.load(f)
            return SaveMetadata(
                save_id=save_id,
                created_at=datetime.fromisoformat(data.get("created_at", "")),
                last_played=datetime.fromisoformat(data.get("last_played", "")),
                total_turns=data.get("total_turns", 0),
                total_playtime_seconds=data.get("playtime_seconds", 0),
                scenario_source=data.get("scenario_source"),
                character_name=data.get("character_name"),
            )
        
        # Fallback: infer from directory stats
        wbx = self.saves_dir / f"{save_id}.wbx"
        stat = wbx.stat() if wbx.exists() else core_metadata.stat()
        return SaveMetadata(
            save_id=save_id,
            created_at=datetime.fromtimestamp(stat.st_ctime),
            last_played=datetime.fromtimestamp(stat.st_mtime),
            total_turns=0,
            total_playtime_seconds=0,
        )
    
    def update_metadata(self, save_id: str, updates: dict):
        """Update metadata fields after a turn completes."""
        metadata = self._load_metadata(save_id)
        for key, value in updates.items():
            setattr(metadata, key, value)
        
        # Persist to Core/metadata.json
        workspace = self.saves_dir / save_id
        core_metadata = workspace / "Core" / "metadata.json"
        existing = {}
        if core_metadata.exists():
            with open(core_metadata, "r") as f:
                existing = json.load(f)
        
        existing.update({
            "last_played": metadata.last_played.isoformat(),
            "total_turns": metadata.total_turns,
            "playtime_seconds": metadata.total_playtime_seconds,
        })
        
        with open(core_metadata, "w") as f:
            json.dump(existing, f, indent=2)
```

### 1.2 Save Switching

```python
# backend/engine/session.py

class GameSessionManager:
    def __init__(self, save_manager: SaveManager, memory_manager: MemoryManager):
        self.save_manager = save_manager
        self.memory_manager = memory_manager
        self.active_save_id: Optional[str] = None
        self.state: Optional[WorldState] = None
        self._play_start_time: Optional[datetime] = None
    
    async def load_save(self, save_id: str) -> WorldState:
        """Load a save, properly closing the previous one."""
        # Close current save
        if self.active_save_id:
            await self._close_current_save()
        
        # Load new save
        state_data = self.save_manager.load(save_id)
        state = WorldState(**state_data)
        
        self.active_save_id = save_id
        self.state = state
        self._play_start_time = datetime.now()
        
        # Initialize memory for this save
        memory_path = self.save_manager.get_workspace(save_id) / "vector_index"
        self.memory_manager.initialize(str(memory_path))
        
        return state
    
    async def _close_current_save(self):
        """Properly close current save: save final state, flush memory."""
        if self.state and self.active_save_id:
            # Save any pending state
            self.save_manager.save_state(self.active_save_id, self.state.model_dump(
                exclude={"sdk", "_lancedb_table"}
            ))
            
            # Update playtime
            if self._play_start_time:
                elapsed = (datetime.now() - self._play_start_time).total_seconds()
                self.save_manager.update_metadata(self.active_save_id, {
                    "total_playtime_seconds": elapsed  # Add to existing
                })
    
    async def create_save(self, save_id: str, template_data: Optional[dict] = None) -> WorldState:
        """Create a new save."""
        state = WorldState(active_save_id=save_id)
        self.save_manager.create(save_id, state.model_dump(
            exclude={"sdk", "_lancedb_table"}
        ))
        return await self.load_save(save_id)
    
    async def delete_save(self, save_id: str) -> bool:
        """Delete a save and its workspace."""
        if save_id == self.active_save_id:
            await self._close_current_save()
            self.active_save_id = None
            self.state = None
        
        return self.save_manager.delete(save_id)
```

### 1.3 API Updates

```python
# backend/api/server.py

@router.get("/api/saves")
async def list_saves():
    """Return all saves with metadata, sorted by last played."""
    saves = save_manager.get_all_saves()
    return {
        "saves": [
            {
                "save_id": s.save_id,
                "created_at": s.created_at.isoformat(),
                "last_played": s.last_played.isoformat(),
                "total_turns": s.total_turns,
                "character_name": s.character_name,
                "scenario_source": s.scenario_source,
                "is_active": s.save_id == session_manager.active_save_id,
            }
            for s in saves
        ]
    }

@router.delete("/api/saves/{save_id}")
async def delete_save(save_id: str):
    """Delete a save."""
    success = await session_manager.delete_save(save_id)
    if not success:
        raise HTTPException(404, f"Save '{save_id}' not found")
    return {"deleted": save_id}

@router.post("/api/saves/{save_id}/export")
async def export_save(save_id: str):
    """Export a save as a downloadable .wbx file."""
    wbx_path = save_manager.pack_wbx(save_id)
    return FileResponse(wbx_path, filename=f"{save_id}.wbx")

@router.post("/api/saves/import")
async def import_save(file: UploadFile):
    """Import a .wbx file as a new save."""
    save_id = save_manager.import_wbx(await file.read(), file.filename)
    return {"imported": save_id}
```

### 1.4 Frontend Updates

Extend the existing save panel in `App.jsx`:

- **Save Browser**: Table/list showing all saves with metadata
- **Active Save Highlight**: Current save visually distinct
- **Create Button**: Opens create-save dialog (name + optional template)
- **Delete Button**: With confirmation dialog
- **Load Button**: On non-active saves
- **Import/Export Buttons**: File upload/download for .wbx sharing
- **Save Metadata Display**: Character name, total turns, last played, playtime

---

## Phase 2: Live LanceDB in .wbx Packing

### Goal

During gameplay, LanceDB writes to a workspace directory. When the user wants to export or the game ends, LanceDB files must be included in the `.wbx` archive.

### 2.1 Workspace Structure Update

```
data/saves/autosave/          # Live workspace (during gameplay)
├── Core/
├── Characters/
├── Module_States/
├── Snapshots/
└── vector_index/              # LanceDB directory (live)
    ├── _latest/
    ├── _versions/
    └── ...

data/saves/autosave.wbx        # Packed archive (on demand or on close)
```

### 2.2 Packing Strategy

```python
# backend/engine/save_manager.py

def pack_wbx(self, save_id: str) -> Path:
    """Pack workspace into .wbx archive, including live LanceDB files."""
    workspace = self.saves_dir / save_id
    wbx_path = self.saves_dir / f"{save_id}.wbx"
    
    # 1. Ensure LanceDB is in a consistent state
    self._flush_lancedb(save_id)
    
    # 2. Write current state to JSONs
    self._write_state_files(save_id)
    
    # 3. Create zip archive
    with zipfile.ZipFile(wbx_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in workspace.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(workspace)
                zf.write(file_path, arcname)
    
    return wbx_path

def _flush_lancedb(self, save_id: str):
    """Ensure LanceDB tables are flushed and consistent before packing."""
    # LanceDB uses WAL/journal files — we need them committed
    # Option: close and reopen the table to flush
    # Option: use LanceDB's optimize/compact operations
    ...
    
def unpack_wbx(self, wbx_data: bytes, save_id: str) -> Path:
    """Unpack .wbx archive into workspace directory."""
    workspace = self.saves_dir / save_id
    workspace.mkdir(parents=True, exist_ok=True)
    
    with zipfile.ZipFile(io.BytesIO(wbx_data)) as zf:
        zf.extractall(workspace)
    
    return workspace
```

### 2.3 LanceDB Consistency During Gameplay

**Problem**: LanceDB uses write-ahead logs and version files. If the server crashes mid-write, the index may be corrupt.

**Solutions**:
1. **Snapshot before packing**: Close LanceDB table, copy files, reopen
2. **Regular auto-pack**: Every N turns, flush and snapshot LanceDB files into the workspace
3. **WAL recovery**: On load, attempt to recover from WAL if needed

```python
def ensure_lancedb_consistent(self, table_path: str):
    """Attempt to open table; if it fails, try recovery or create new."""
    try:
        import lancedb
        db = lancedb.connect(table_path)
        db.table_names()  # This will fail if corrupt
        return db
    except Exception:
        # Recovery attempt: remove WAL files, try again
        # If still fails: create new table, warn user about lost vector data
        logger.warning("LanceDB may be corrupt; attempting recovery...")
        ...
```

---

## Phase 3: Multi-Session Support (Future)

### Current State

Single-user, single-session: one browser client at a time.

### Multi-Session Design

For multi-user support (e.g., different players on different tabs, or a DM + player setup):

```
Server
├── SessionManager
│   ├── sessions: dict[str, GameSessionManager]
│   │   ├── "session_abc123" → GameSessionManager (Player 1, save: "campaign_1")
│   │   ├── "session_def456" → GameSessionManager (Player 2, save: "one_shot")
│   │   └── "session_ghi789" → GameSessionManager (DM, save: "campaign_1")  # Same save, different session
│   └── saves: dict[str, SaveMetadata]  # Save-level locking
```

### 3.1 Session Isolation

```python
class SessionManager:
    def __init__(self):
        self._sessions: dict[str, GameSessionManager] = {}
        self._save_locks: dict[str, str] = {}  # save_id → session_id (write lock)
    
    def create_session(self) -> str:
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = GameSessionManager(
            save_manager=SaveManager(),
            memory_manager=MemoryManager()
        )
        return session_id
    
    def get_session(self, session_id: str) -> Optional[GameSessionManager]:
        return self._sessions.get(session_id)
    
    async def load_save_for_session(self, session_id: str, save_id: str) -> bool:
        """Load a save into a session, acquiring a lock."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        
        # Release previous lock
        if session.active_save_id and session.active_save_id in self._save_locks:
            if self._save_locks[session.active_save_id] == session_id:
                del self._save_locks[session.active_save_id]
        
        # Acquire new lock
        if save_id in self._save_locks and self._save_locks[save_id] != session_id:
            # Another session owns this save — load read-only or reject
            logger.warning(f"Save '{save_id}' is locked by another session")
            # Option: load read-only, or return error
            return False
        
        self._save_locks[save_id] = session_id
        await session.load_save(save_id)
        return True
```

### 3.2 WebSocket Session Binding

```python
# backend/api/server.py

@socket_app.on("connect")
async def on_connect(socket: WebSocket):
    await socket.accept()
    session_id = socket.headers.get("x-session-id") or str(uuid.uuid4())
    socket.session_id = session_id
    socket.game_session = session_manager.create_session()
    await socket.send_json({"type": "connected", "session_id": session_id})
```

### 3.3 Concurrent Save Access Strategy

For multiple sessions accessing the same save:

| Access Pattern | Strategy |
|---|---|
| One session per save (single-player) | Full read/write |
| Multiple sessions, same save (DM + player) | Write lock to DM, read-only to players |
| Multiple sessions, different saves | Full isolation |

---

## Execution Order

1. **Phase 1.1**: Save metadata tracking (backend only)
2. **Phase 1.3**: API updates for multi-save (list with metadata, delete, export, import)
3. **Phase 1.4**: Frontend save browser (list with metadata, create, delete, import/export)
4. **Phase 2**: Live LanceDB packing (flush + include in .wbx)
5. **Phase 3**: Multi-session (deferred until single-session multi-save is stable)

---

## Testing

- Test: Create 3 saves, list them, verify metadata
- Test: Switch between saves, verify state isolation
- Test: Delete save, verify workspace removed
- Test: Export save as .wbx, import on fresh instance, verify state
- Test: Playtime tracking updates on save switch
- Test: LanceDB .wbx packing includes all files
- Test: Corrupt LanceDB recovery falls back gracefully
- (Phase 3): Two WebSocket connections, same save, read-only enforcement
