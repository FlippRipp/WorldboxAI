import json
import re
from pathlib import Path
from typing import Any


# The four semantic colors the UI theme is built from. Defaults match the
# original hardcoded look (purple accent, dark gray surfaces, light gray text,
# tan in-story dialogue) so a fresh install looks identical to before theming.
DEFAULT_COLORS: dict[str, str] = {
    "primary": "#9333ea",     # purple-600 accent
    "background": "#111827",   # gray-900 base surface
    "text": "#e5e7eb",         # gray-200 base body text
    "dialogue": "#d4a574",     # in-story quoted speech (.text-quote)
}

DEFAULT_THEME: dict[str, Any] = {
    "preset": "default",
    "colors": dict(DEFAULT_COLORS),
}

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _valid_hex(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX_RE.match(value.strip()))


class ThemeStore:
    """Persists the global UI theme to data/theme.json.

    Mirrors the global-JSON pattern used for the prompt pipeline
    (save_manager.load/save_global_prompt_pipeline).
    """

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)

    def _path(self) -> Path:
        return self.data_dir / "theme.json"

    def load(self) -> dict[str, Any]:
        path = self._path()
        if not path.exists():
            return self._default()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return self._normalize(raw)
        except (json.JSONDecodeError, OSError, ValueError):
            # Corrupt or unreadable file → fall back to the default theme.
            return self._default()

    def save(self, theme: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize(theme)
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2)
        return normalized

    def _default(self) -> dict[str, Any]:
        return json.loads(json.dumps(DEFAULT_THEME))

    def _normalize(self, theme: Any) -> dict[str, Any]:
        if not isinstance(theme, dict):
            return self._default()
        incoming = theme.get("colors")
        colors = dict(DEFAULT_COLORS)
        if isinstance(incoming, dict):
            for key in DEFAULT_COLORS:
                val = incoming.get(key)
                if _valid_hex(val):
                    colors[key] = val.strip()
        preset = theme.get("preset")
        if not isinstance(preset, str) or not preset:
            preset = "custom"
        return {"preset": preset, "colors": colors}
