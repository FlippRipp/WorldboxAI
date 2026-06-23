import json
import os
import logging
from pathlib import Path
from typing import Any
from backend.engine.providers import PROVIDERS

logger = logging.getLogger(__name__)

ACTIVE_PROVIDER_FILE = "active_provider.json"


class ProviderManager:
    def __init__(self, providers_dir: str = "data/providers"):
        self._dir = Path(providers_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._active_id = self._load_active()
        self._llm_service = None

    def set_llm_service(self, llm_service):
        self._llm_service = llm_service
        print(f"[DEBUG] ProviderManager.set_llm_service: active_id='{self._active_id}'")
        if self._active_id:
            config = self.get_effective_config(self._active_id)
            self._apply_api_keys(self._active_id, config)
            print(f"[DEBUG] ProviderManager.set_llm_service: reconfigure with reader_model='{config.get('reader_model')}'")
            llm_service.reconfigure(self._active_id, config)

    def get_all(self) -> list[dict]:
        providers = []
        for pid, pdef in PROVIDERS.items():
            config = self.get_config(pid)
            models = {
                k: config.get(k, fdef.get("default", ""))
                for k, fdef in pdef["fields"].items()
                if fdef["type"] in ("text", "slider", "select")
            }
            providers.append({
                "id": pid,
                "label": pdef["label"],
                "litellm_prefix": pdef["litellm_prefix"],
                "active": pid == self._active_id,
                "models": models,
                "fields": pdef["fields"],
                "presets": pdef.get("presets", []),
            })
        return providers

    def get_active(self) -> str:
        return self._active_id or "gemini"

    def set_active(self, provider_id: str):
        if provider_id not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider_id}")
        self._active_id = provider_id
        self._save_active()
        config = self.get_effective_config(provider_id)
        print(f"[DEBUG] ProviderManager.set_active: provider_id='{provider_id}', reader_model='{config.get('reader_model')}', storyteller_model='{config.get('storyteller_model')}'")
        self._apply_api_keys(provider_id, config)
        if self._llm_service:
            self._llm_service.reconfigure(provider_id, config)

    def get_config(self, provider_id: str) -> dict:
        if provider_id not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider_id}")
        path = self._dir / provider_id / "config.json"
        overrides = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    overrides = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        defaults = {}
        for key, fdef in PROVIDERS[provider_id]["fields"].items():
            defaults[key] = fdef.get("default", "")
        defaults.update(overrides)
        return defaults

    def save_config(self, provider_id: str, updates: dict):
        if provider_id not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider_id}")
        provider_dir = self._dir / provider_id
        provider_dir.mkdir(parents=True, exist_ok=True)
        path = provider_dir / "config.json"
        existing = self.get_config(provider_id)
        existing.update(updates)
        valid_keys = set(PROVIDERS[provider_id]["fields"].keys())
        cleaned = {k: v for k, v in existing.items() if k in valid_keys}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2, default=str)
        self._apply_api_keys(provider_id, cleaned)
        if provider_id == self._active_id and self._llm_service:
            config = self.get_effective_config(provider_id)
            print(f"[DEBUG] ProviderManager.save_config: provider_id='{provider_id}', reader_model raw='{cleaned.get('reader_model')}' effective='{config.get('reader_model')}'")
            self._llm_service.reconfigure(provider_id, config)

    def apply_preset(self, provider_id: str, preset_label: str) -> dict:
        if provider_id not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider_id}")
        for preset in PROVIDERS[provider_id].get("presets", []):
            if preset["label"] == preset_label:
                models = dict(preset.get("config", preset.get("models", {})))
                self.save_config(provider_id, models)
                return self.get_config(provider_id)
        raise ValueError(f"Preset '{preset_label}' not found for provider '{provider_id}'")

    def _assemble_model_string(self, provider_id: str, raw_name: str) -> str:
        if provider_id != "openrouter" or "/" in raw_name:
            return raw_name
        config = self.get_config(provider_id)
        or_provider = config.get("openrouter_provider", "anthropic")
        return f"openrouter/{or_provider}/{raw_name}"

    def get_effective_config(self, provider_id: str) -> dict:
        config = dict(self.get_config(provider_id))
        if provider_id == "openrouter":
            model_fields = [
                "storyteller_model",
                "reader_model",
                "embedding_model",
                "module_fast_model",
                "storyteller_fallback_models",
            ]
            print("[DEBUG] get_effective_config: provider=openrouter")
            for field in model_fields:
                val = config.get(field, "")
                if not val:
                    if field != "storyteller_fallback_models":
                        print(f"[DEBUG] get_effective_config: {field} is empty, skipping")
                    continue
                if val.startswith("openrouter/"):
                    print(f"[DEBUG] get_effective_config: {field} already has openrouter/ prefix: '{val}'")
                    continue
                if field == "storyteller_fallback_models":
                    models = [m.strip() for m in val.split(",") if m.strip()]
                    new_val = ",".join(f"openrouter/{m}" for m in models)
                    print(f"[DEBUG] get_effective_config: {field} raw='{val}' -> effective='{new_val}'")
                    config[field] = new_val
                else:
                    new_val = f"openrouter/{val}"
                    print(f"[DEBUG] get_effective_config: {field} raw='{val}' -> effective='{new_val}'")
                    config[field] = new_val
        return config

    async def test_connection(self, provider_id: str) -> dict:
        if provider_id not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider_id}")
        try:
            import litellm
            config = self.get_effective_config(provider_id)
            api_key = config.get("api_key", "")
            if not api_key:
                return {"success": False, "error": "No API key configured"}

            test_model = config.get("reader_model") or config.get("storyteller_model", "")
            if not test_model:
                return {"success": False, "error": "No model configured"}

            response = await litellm.acompletion(
                model=test_model,
                messages=[{"role": "user", "content": "Say 'ok' and nothing else."}],
                max_tokens=5,
            )
            return {"success": True, "model": test_model}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def fetch_models(self, provider_id: str) -> dict:
        if provider_id not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider_id}")
        try:
            import litellm
            config = self.get_config(provider_id)
            api_key = config.get("api_key", "")
            if not api_key:
                return {"models": [], "error": "No API key configured"}
            pdef = PROVIDERS[provider_id]
            method = pdef.get("model_fetch_method", "generic")
            if method == "openai":
                import openai
                client = openai.OpenAI(api_key=api_key)
                raw = client.models.list()
                models = sorted(
                    [{"id": m.id, "is_embedding": "embedding" in m.id.lower(), "provider": None}
                     for m in raw.data],
                    key=lambda x: x["id"].lower()
                )
            elif method == "deepseek":
                import openai
                base_url = pdef.get("base_url", "https://api.deepseek.com")
                client = openai.OpenAI(api_key=api_key, base_url=base_url)
                raw = client.models.list()
                models = sorted(
                    [{"id": m.id, "is_embedding": "embedding" in m.id.lower(), "provider": None}
                     for m in raw.data],
                    key=lambda x: x["id"].lower()
                )
            elif method == "openrouter":
                import httpx
                async with httpx.AsyncClient() as client:
                    resp = await client.get("https://openrouter.ai/api/v1/models?output_modalities=all")
                    data = resp.json()
                    raw_models = []
                    for m in data.get("data", []):
                        mid = m.get("id", "")
                        arch = m.get("architecture", {}) if isinstance(m.get("architecture"), dict) else {}
                        output_modalities = arch.get("output_modalities", [])
                        is_emb = "embeddings" in output_modalities or "embed" in mid.lower()
                        provider = mid.split("/")[0] if "/" in mid else None
                        raw_models.append({"id": mid, "is_embedding": is_emb, "provider": provider})
                    models = sorted(raw_models, key=lambda x: x["id"].lower())
            elif method == "gemini":
                litellm.api_key = api_key
                litellm_resp = litellm.model_list()
                seen = {}
                for m in litellm_resp.get("data", []):
                    mid = m.get("id", "")
                    if mid:
                        seen[mid] = {"id": mid, "is_embedding": "embedding" in mid.lower(), "provider": None}
                models = sorted(seen.values(), key=lambda x: x["id"].lower())
            elif method == "anthropic":
                models = sorted(
                    [
                        {"id": "claude-sonnet-4-20250514", "is_embedding": False, "provider": None},
                        {"id": "claude-opus-4-20250514", "is_embedding": False, "provider": None},
                        {"id": "claude-haiku-4-20250514", "is_embedding": False, "provider": None},
                    ],
                    key=lambda x: x["id"].lower()
                )
            else:
                models = []
            return {"models": models, "error": None}
        except Exception as e:
            return {"models": [], "error": str(e)}

    def _apply_api_keys(self, provider_id: str, config: dict):
        api_key = config.get("api_key", "")
        pdef = PROVIDERS.get(provider_id, {})
        env_var = (pdef.get("fields", {}).get("api_key", {}).get("env_var", ""))
        if env_var and api_key:
            os.environ[env_var] = api_key

    def _load_active(self) -> str:
        path = self._dir / ACTIVE_PROVIDER_FILE
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    pid = data.get("active", "gemini")
                    if pid in PROVIDERS:
                        return pid
            except (json.JSONDecodeError, OSError):
                pass
        return "gemini"

    def _save_active(self):
        path = self._dir / ACTIVE_PROVIDER_FILE
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"active": self._active_id}, f, indent=2)
