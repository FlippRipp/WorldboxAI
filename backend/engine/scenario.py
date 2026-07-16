"""Basic scenario store — the default story source when no module overrides it.

A *scenario* is the minimal thing needed to start a story without a generated
world:

- ``scenario_description``: a system prompt describing the setting/situation to
  the AI (the equivalent of the world's compiled rules + lore).
- ``starting_prompt``: the literal first AI message shown to the player. When
  provided, the story opens with this text verbatim (no LLM call). When empty,
  the opening scene is generated from ``scenario_description``.

Scenarios live as flat JSON files under ``data/scenarios/{id}.json``, mirroring
the world persistence layout so the two story sources behave symmetrically.
"""

import json
import re
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "scenario"


class ScenarioStore:
    def __init__(self, data_dir):
        self.scenarios_dir = Path(data_dir) / "scenarios"

    def _path(self, scenario_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_]+", scenario_id or ""):
            raise ValueError(f"Invalid scenario id: {scenario_id!r}")
        return self.scenarios_dir / f"{scenario_id}.json"

    def list_scenarios(self) -> list[dict]:
        if not self.scenarios_dir.exists():
            return []
        out = []
        for path in sorted(self.scenarios_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                out.append({
                    "id": data.get("id", path.stem),
                    "name": data.get("name", path.stem),
                    "created_at": data.get("created_at"),
                    "has_starting_prompt": bool(data.get("starting_prompt", "").strip()),
                })
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping unreadable scenario %s: %s", path.name, exc)
        return out

    def load_scenario(self, scenario_id: str) -> dict:
        path = self._path(scenario_id)
        if not path.exists():
            raise FileNotFoundError(f"Scenario '{scenario_id}' not found.")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_scenario(self, data: dict) -> dict:
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("Scenario name is required.")
        scenario_id = data.get("id") or self._unique_id(_slugify(name))
        record = {
            "id": scenario_id,
            "name": name,
            "scenario_description": data.get("scenario_description", ""),
            "starting_prompt": data.get("starting_prompt", ""),
            # Story-style direction seeded onto saves created from this
            # scenario; each is free text and empty by default.
            "themes": data.get("themes", ""),
            "tags": data.get("tags", ""),
            "pacing": data.get("pacing", ""),
            # Module defaults seeded onto saves created from this scenario.
            # active_modules: list of module ids, or None meaning "unset"
            # (story creation decides). module_instructions: per-module
            # instruction-slot overrides, {mod_id: {slot_id: text}}.
            "active_modules": data.get("active_modules") if isinstance(data.get("active_modules"), list) else None,
            "module_instructions": data.get("module_instructions") or {},
            "created_at": data.get("created_at") or datetime.now(timezone.utc).isoformat(),
        }
        self.scenarios_dir.mkdir(parents=True, exist_ok=True)
        with open(self._path(scenario_id), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        return record

    def delete_scenario(self, scenario_id: str) -> None:
        path = self._path(scenario_id)
        if path.exists():
            path.unlink()

    def _unique_id(self, base: str) -> str:
        candidate = base
        i = 2
        while (self.scenarios_dir / f"{candidate}.json").exists():
            candidate = f"{base}_{i}"
            i += 1
        return candidate
