"""Persistent API usage and cost tracking across LLM, embedding, search, Jev, and cloud image calls.

Provides:
- ``CostCalculator``: layered USD cost resolution (provider-reported -> cached
  router catalog rates -> LiteLLM cost map -> service-specific flat rates).
- ``UsageLedger``: thread-safe SQLite store (``llm_usage.db``) recording compact
  call rows, backfilling historical ``llm_calls.jsonl`` entries on first run,
  and computing filtered totals, breakdowns, and time-bucketed base +
  accumulated spend series.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import litellm

USAGE_DB_FILENAME = "llm_usage.db"
PRICING_CACHE_FILENAME = "pricing_cache.json"

# Flat per-request search / decision / cloud-image fallback rates documented in
# backend/engine/providers.py, backend/engine/jev.py, and Novita's API tiers.
OPENROUTER_SEARCH_FEE_USD = 0.005
NANOGPT_SEARCH_FEE_USD = 0.006
JEV_INPUT_USD_PER_MILLION = 0.042

NOVITA_IMAGE_RATES_USD: dict[str, float] = {
    "flux": 0.03,
    "sdxl": 0.006,
    "pony": 0.006,
    "illustrious": 0.006,
    "noobai": 0.006,
    "sd15": 0.0015,
}

DEFAULT_MODULE_LABELS: dict[str, str] = {
    "core": "Core Engine",
    "wb_worldgen": "World Builder",
    "wb_core_rpg": "Core RPG",
    "wb_npc_system": "NPC System",
    "wb_plot_director": "Plot Director",
    "wb_character_tracker": "Character Tracker",
    "wb_image_gen": "Image Studio",
}

_WORLDGEN_STEP_PREFIXES = (
    "world_build",
    "agent:",
    "enrich:",
    "map:",
    "site:",
    "travel_intent",
    "intro:known_locations",
)

_ROUTER_PREFIXES = ("openrouter/", "nano-gpt/", "nanogpt/")


def _finite_nonneg(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def normalize_module_source(module_source: str | None, call_type: str = "", step: str = "") -> str:
    """Map every call to a clean, filterable module identifier.

    Explicit module IDs (``wb_*``) pass through unchanged. Calls made directly
    by worldgen without ``LLMBridge`` are attributed to ``wb_worldgen``; core
    engine pipelines (storyteller, reader, librarian, character builder, etc.)
    map to ``core``.
    """
    mod = str(module_source or "").strip()
    if mod and mod not in ("module", "diagnostic"):
        return mod
    ct = str(call_type or "").strip()
    st = str(step or "").strip()
    if ct in ("world_build", "world_ideation") or st.startswith(_WORLDGEN_STEP_PREFIXES):
        return "wb_worldgen"
    if ct == "image_gen":
        return "wb_image_gen"
    return "core"


def infer_provider(model: str, explicit_provider: str = "", call_type: str = "") -> str:
    prov = str(explicit_provider or "").strip().lower()
    if prov:
        return "nanogpt" if prov == "nano-gpt" else prov
    m = str(model or "").strip().lower()
    if m == "mock":
        return "mock"
    if call_type == "decision" or "jev" in m:
        return "jev"
    if m.startswith("openrouter/"):
        return "openrouter"
    if m.startswith(("nano-gpt/", "nanogpt/")):
        return "nanogpt"
    if m.startswith("novita/"):
        return "novita"
    if "/" in m:
        prefix = m.split("/", 1)[0]
        if prefix in ("gemini", "openai", "anthropic", "deepseek"):
            return prefix
    return ""


class CostCalculator:
    """Layered USD cost resolver with optional on-disk router pricing cache."""

    def __init__(self, cache_path: str | Path | None = None):
        self._cache_path = Path(cache_path) if cache_path else None
        self._lock = threading.Lock()
        # model key -> {"input": float_per_token, "output": float_per_token, "request": float_per_call}
        self._router_rates: dict[str, dict[str, float]] = {}
        self._load_cache()

    def _load_cache(self):
        if not self._cache_path or not self._cache_path.exists():
            return
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("rates"), dict):
                with self._lock:
                    for k, v in raw["rates"].items():
                        if isinstance(k, str) and isinstance(v, dict):
                            inp = _finite_nonneg(v.get("input"))
                            out = _finite_nonneg(v.get("output"))
                            req = _finite_nonneg(v.get("request")) or 0.0
                            if inp is not None and out is not None:
                                self._router_rates[k] = {"input": inp, "output": out, "request": req}
        except (OSError, ValueError):
            pass

    def _save_cache(self):
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = {"updated_at": time.time(), "rates": dict(self._router_rates)}
            fd, tmp_name = tempfile.mkstemp(dir=self._cache_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                os.replace(tmp_name, self._cache_path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        except OSError:
            pass

    def update_openrouter_models(self, models_data: list[dict]):
        """Ingest OpenRouter ``/api/v1/models`` entries (USD per token)."""
        updated = False
        with self._lock:
            for item in models_data or []:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or "").strip()
                pricing = item.get("pricing")
                if not mid or not isinstance(pricing, dict):
                    continue
                inp = _finite_nonneg(pricing.get("prompt"))
                out = _finite_nonneg(pricing.get("completion"))
                if inp is None and out is None:
                    continue
                rate = {
                    "input": inp or 0.0,
                    "output": out or 0.0,
                    "request": _finite_nonneg(pricing.get("request")) or 0.0,
                }
                self._router_rates[f"openrouter/{mid}"] = rate
                updated = True
        if updated:
            self._save_cache()

    def update_nanogpt_models(self, models_data: list[dict]):
        """Ingest NanoGPT ``/api/v1/models`` or ``/api/v1/embedding-models`` entries.

        NanoGPT publishes ``"unit": "per_million_tokens"`` (e.g. ``prompt: 2``
        = $2 / 1M tokens); normalize to USD per single token.
        """
        updated = False
        with self._lock:
            for item in models_data or []:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or item.get("model") or "").strip()
                pricing = item.get("pricing")
                if not mid or not isinstance(pricing, dict):
                    continue
                inp = _finite_nonneg(pricing.get("prompt") if "prompt" in pricing else pricing.get("input"))
                out = _finite_nonneg(pricing.get("completion") if "completion" in pricing else pricing.get("output"))
                if inp is None and out is None:
                    continue
                unit = str(pricing.get("unit") or "").strip().lower()
                scale = (1.0 / 1_000_000.0) if "million" in unit else 1.0
                rate = {
                    "input": (inp or 0.0) * scale,
                    "output": (out or 0.0) * scale,
                    "request": _finite_nonneg(pricing.get("request")) or 0.0,
                }
                self._router_rates[f"nano-gpt/{mid}"] = rate
                self._router_rates[f"nanogpt/{mid}"] = rate
                updated = True
        if updated:
            self._save_cache()

    def has_router_rate(self, model: str, provider: str = "") -> bool:
        return self._lookup_router_rate(model, provider) is not None

    def _lookup_router_rate(self, model: str, provider: str = "") -> dict[str, float] | None:
        m = str(model or "").strip()
        if not m:
            return None
        prov = infer_provider(m, provider)
        candidates = [m]
        stripped = m
        for prefix in _ROUTER_PREFIXES:
            if stripped.startswith(prefix):
                stripped = stripped.removeprefix(prefix)
                break
        if prov == "openrouter":
            candidates.append(f"openrouter/{stripped}")
        elif prov == "nanogpt":
            candidates.append(f"nano-gpt/{stripped}")
            candidates.append(f"nanogpt/{stripped}")
        with self._lock:
            for cand in candidates:
                if cand in self._router_rates:
                    return self._router_rates[cand]
        return None

    @staticmethod
    def _extract_reported_cost(usage: dict | None) -> float | None:
        if not isinstance(usage, dict):
            return None
        for key in ("cost", "total_cost"):
            val = _finite_nonneg(usage.get(key))
            if val is not None:
                return val
        cost_details = usage.get("cost_details")
        if isinstance(cost_details, dict):
            val = _finite_nonneg(cost_details.get("upstream_inference_cost") or cost_details.get("total_cost"))
            if val is not None:
                return val
        return None

    @staticmethod
    def _litellm_has_model(candidate: str) -> bool:
        if not candidate:
            return False
        if candidate in litellm.model_cost:
            return True
        try:
            info = litellm.get_model_info(model=candidate)
            key = info.get("key") if isinstance(info, dict) else None
            return bool(key and key in litellm.model_cost)
        except Exception:
            return False

    def _litellm_cost(self, model: str, tokens_in: int, tokens_out: int, response: Any = None) -> float | None:
        # 1. Check LiteLLM's pre-computed response_cost if the model is genuinely known.
        if response is not None:
            hidden = getattr(response, "_hidden_params", None)
            if isinstance(hidden, dict):
                rc = _finite_nonneg(hidden.get("response_cost"))
                if rc is not None and (rc > 0 or self._litellm_has_model(model)):
                    return rc
            if self._litellm_has_model(model):
                try:
                    c = _finite_nonneg(litellm.completion_cost(completion_response=response, model=model))
                    if c is not None:
                        return c
                except Exception:
                    pass

        if tokens_in <= 0 and tokens_out <= 0:
            return 0.0 if self._litellm_has_model(model) else None

        candidates = [model]
        for prefix in _ROUTER_PREFIXES:
            if model.startswith(prefix):
                stripped = model.removeprefix(prefix)
                if stripped:
                    candidates.append(stripped)
                    # Author-prefixed router IDs like "google/gemini-2.5-flash" -> "gemini/gemini-2.5-flash"
                    if stripped.startswith("google/"):
                        candidates.append("gemini/" + stripped.removeprefix("google/"))
                break

        for cand in candidates:
            if not self._litellm_has_model(cand):
                continue
            try:
                in_cost, out_cost = litellm.cost_per_token(
                    model=cand,
                    prompt_tokens=max(0, int(tokens_in)),
                    completion_tokens=max(0, int(tokens_out)),
                )
                total = _finite_nonneg((in_cost or 0.0) + (out_cost or 0.0))
                if total is not None:
                    return total
            except Exception:
                continue
        return None

    @staticmethod
    def estimate_novita_image_cost(model_name: str = "", family: str = "") -> float:
        fam = str(family or "").strip().lower()
        m = str(model_name or "").strip().lower()
        if "flux" in fam or "flux" in m:
            return NOVITA_IMAGE_RATES_USD["flux"]
        if fam in NOVITA_IMAGE_RATES_USD:
            return NOVITA_IMAGE_RATES_USD[fam]
        if any(tag in m for tag in ("xl", "pony", "illustrious", "noob")):
            return NOVITA_IMAGE_RATES_USD["sdxl"]
        return NOVITA_IMAGE_RATES_USD["sd15"]

    def resolve_cost(
        self,
        *,
        model: str,
        provider: str = "",
        call_type: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        usage: dict | None = None,
        response: Any = None,
        extra_cost_usd: float = 0.0,
        extra_included_in_reported: bool = False,
        image_family: str = "",
    ) -> tuple[float | None, str]:
        """Resolve ``(cost_usd, cost_source)`` for an API call.

        ``cost_source`` is one of ``"reported"``, ``"priced"``, ``"free"``, or
        ``"unknown"``.
        """
        m = str(model or "").strip()
        prov = infer_provider(m, provider, call_type)
        extra = float(extra_cost_usd or 0.0)

        if m in ("mock", "") and prov in ("mock", "local"):
            return 0.0, "free"
        if m == "mock" or prov == "local":
            return 0.0, "free"

        # 1. Direct upstream-reported cost in usage dict
        reported = self._extract_reported_cost(usage)
        if reported is not None:
            add = 0.0 if extra_included_in_reported else extra
            return round(reported + add, 9), "reported"

        # 2. Cloud image generation fallback (Novita)
        if call_type == "image_gen" and prov == "novita":
            return round(self.estimate_novita_image_cost(m, image_family) + extra, 9), "priced"

        # 3. Jev decision fallback
        if call_type == "decision" or prov == "jev":
            if tokens_in > 0:
                return round((tokens_in * JEV_INPUT_USD_PER_MILLION / 1_000_000.0) + extra, 9), "priced"
            return (round(extra, 9), "priced") if extra > 0 else (None, "unknown")

        # 4. Cached dynamic router rates (OpenRouter / NanoGPT)
        rate = self._lookup_router_rate(m, prov)
        if rate is not None:
            if tokens_in > 0 or tokens_out > 0 or rate["request"] > 0:
                token_cost = (
                    max(0, int(tokens_in)) * rate["input"]
                    + max(0, int(tokens_out)) * rate["output"]
                    + rate["request"]
                )
                return round(token_cost + extra, 9), "priced"
            if extra > 0:
                return round(extra, 9), "priced"

        # 5. LiteLLM static cost table
        ll_cost = self._litellm_cost(m, tokens_in, tokens_out, response=response)
        if ll_cost is not None:
            return round(ll_cost + extra, 9), "priced"

        if extra > 0:
            return round(extra, 9), "priced"
        return None, "unknown"


class UsageLedger:
    """Persistent SQLite ledger for API usage analytics and time-series queries."""

    def __init__(
        self,
        log_dir: str | Path,
        cost_calculator: CostCalculator | None = None,
        backfill_jsonl_path: str | Path | None = None,
    ):
        self.log_dir = Path(log_dir)
        self.db_path = self.log_dir / USAGE_DB_FILENAME
        self.cost_calculator = cost_calculator or CostCalculator()
        self._lock = threading.Lock()
        self._initialized = False
        self._backfill_path = Path(backfill_jsonl_path) if backfill_jsonl_path else None

    def _connect(self) -> sqlite3.Connection:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_initialized(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_calls (
                        id TEXT PRIMARY KEY,
                        timestamp REAL NOT NULL,
                        provider TEXT NOT NULL DEFAULT '',
                        model TEXT NOT NULL DEFAULT '',
                        module_source TEXT NOT NULL DEFAULT 'core',
                        call_type TEXT NOT NULL DEFAULT '',
                        step TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'complete',
                        tokens_in INTEGER NOT NULL DEFAULT 0,
                        tokens_out INTEGER NOT NULL DEFAULT 0,
                        cost_usd REAL,
                        cost_source TEXT NOT NULL DEFAULT 'unknown'
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_calls(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_calls(model)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_module ON usage_calls(module_source)")
                count = conn.execute("SELECT COUNT(*) FROM usage_calls").fetchone()[0]
                if count == 0 and self._backfill_path and self._backfill_path.exists():
                    self._backfill_from_jsonl(conn, self._backfill_path)
                conn.commit()
            self._initialized = True

    def _backfill_from_jsonl(self, conn: sqlite3.Connection, jsonl_path: Path):
        """One-time import of pre-existing ``llm_calls.jsonl`` rows."""
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                rows = []
                for idx, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict) or not rec.get("timestamp"):
                        continue
                    rows.append(self._row_tuple(rec, fallback_id=f"bf_{idx}"))
                if rows:
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO usage_calls
                        (id, timestamp, provider, model, module_source, call_type, step,
                         status, tokens_in, tokens_out, cost_usd, cost_source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
        except OSError:
            pass

    def _row_tuple(self, record: dict, fallback_id: str = "") -> tuple:
        cid = str(record.get("id") or fallback_id or f"u_{time.time_ns()}")
        ts = float(record.get("timestamp") or time.time())
        model = str(record.get("model") or "").strip()
        call_type = str(record.get("call_type") or "").strip()
        step = str(record.get("step") or "").strip()
        status = str(record.get("status") or "complete").strip()
        provider = infer_provider(model, record.get("provider") or "", call_type)
        module_src = normalize_module_source(record.get("module_source"), call_type, step)
        tokens_in = max(0, int(record.get("tokens_in") or 0))
        tokens_out = max(0, int(record.get("tokens_out") or 0))

        cost_usd = _finite_nonneg(record.get("estimated_cost_usd"))
        cost_source = str(record.get("cost_source") or "").strip()

        if cost_usd is None:
            # Cancelled/failed calls that consumed 0 tokens incurred $0.
            if status in ("cancelled", "error") and tokens_in == 0 and tokens_out == 0 and call_type != "search":
                cost_usd, cost_source = 0.0, "free"
            else:
                extra = 0.0
                if call_type == "search" and status == "complete":
                    if provider == "openrouter":
                        extra = OPENROUTER_SEARCH_FEE_USD
                    elif provider == "nanogpt":
                        extra = NANOGPT_SEARCH_FEE_USD
                cost_usd, cost_source = self.cost_calculator.resolve_cost(
                    model=model,
                    provider=provider,
                    call_type=call_type,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    extra_cost_usd=extra,
                )
        elif not cost_source:
            cost_source = "free" if model == "mock" else "priced"

        return (
            cid,
            ts,
            provider,
            model,
            module_src,
            call_type,
            step,
            status,
            tokens_in,
            tokens_out,
            cost_usd,
            cost_source or "unknown",
        )

    def record_call(self, record: dict):
        """Persist one completed call record."""
        self._ensure_initialized()
        row = self._row_tuple(record)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO usage_calls
                    (id, timestamp, provider, model, module_source, call_type, step,
                     status, tokens_in, tokens_out, cost_usd, cost_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                conn.commit()

    def clear(self):
        self._ensure_initialized()
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM usage_calls")
                conn.commit()

    def query_summary(
        self,
        *,
        range_key: str = "30d",
        models: list[str] | None = None,
        modules: list[str] | None = None,
        module_labels: dict[str, str] | None = None,
        tz_offset_minutes: int = 0,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        """Compute filtered totals, base + accumulated time buckets, and breakdowns."""
        self._ensure_initialized()
        now = float(now_ts if now_ts is not None else time.time())
        labels_map = {**DEFAULT_MODULE_LABELS, **(module_labels or {})}

        range_key = (range_key or "30d").strip().lower()
        window_seconds = {
            "24h": 24 * 3600,
            "7d": 7 * 86400,
            "30d": 30 * 86400,
            "90d": 90 * 86400,
        }.get(range_key)
        since_ts = (now - window_seconds) if window_seconds is not None else None

        model_filter = [m.strip() for m in (models or []) if m and m.strip()]
        module_filter = [m.strip() for m in (modules or []) if m and m.strip()]

        def _build_where(include_time: bool, include_model: bool, include_module: bool):
            clauses = []
            params: list[Any] = []
            if include_time and since_ts is not None:
                clauses.append("timestamp >= ?")
                params.append(since_ts)
            if include_model and model_filter:
                placeholders = ",".join("?" for _ in model_filter)
                clauses.append(f"model IN ({placeholders})")
                params.extend(model_filter)
            if include_module and module_filter:
                placeholders = ",".join("?" for _ in module_filter)
                clauses.append(f"module_source IN ({placeholders})")
                params.extend(module_filter)
            where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            return where_sql, params

        with self._lock:
            with self._connect() as conn:
                # Distinct filter choices across the entire ledger so active filters never hide options.
                avail_models = [
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT model FROM usage_calls WHERE model != '' ORDER BY LOWER(model)"
                    ).fetchall()
                ]
                avail_mod_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT module_source FROM usage_calls WHERE module_source != '' ORDER BY module_source"
                    ).fetchall()
                ]

                # All-time total under the same model/module filter (reference tally when viewing 24h/7d/30d).
                where_alltime, params_alltime = _build_where(
                    include_time=False, include_model=True, include_module=True
                )
                alltime_row = conn.execute(
                    f"""
                    SELECT COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                           COUNT(*) AS calls
                    FROM usage_calls
                    {where_alltime}
                    """,
                    params_alltime,
                ).fetchone()

                # Prior cost before window start (so callers can inspect lifetime offset if needed).
                prior_cost_usd = 0.0
                if since_ts is not None:
                    prior_clauses = ["timestamp < ?"]
                    prior_params: list[Any] = [since_ts]
                    if model_filter:
                        prior_clauses.append(f"model IN ({','.join('?' for _ in model_filter)})")
                        prior_params.extend(model_filter)
                    if module_filter:
                        prior_clauses.append(f"module_source IN ({','.join('?' for _ in module_filter)})")
                        prior_params.extend(module_filter)
                    prior_row = conn.execute(
                        f"SELECT COALESCE(SUM(cost_usd), 0.0) FROM usage_calls WHERE {' AND '.join(prior_clauses)}",
                        prior_params,
                    ).fetchone()
                    prior_cost_usd = float(prior_row[0] or 0.0)

                # Filtered rows in the active range
                where_main, params_main = _build_where(
                    include_time=True, include_model=True, include_module=True
                )
                rows = conn.execute(
                    f"""
                    SELECT timestamp, provider, model, module_source, call_type,
                           status, tokens_in, tokens_out, cost_usd, cost_source
                    FROM usage_calls
                    {where_main}
                    ORDER BY timestamp ASC
                    """,
                    params_main,
                ).fetchall()

                # Breakdown by model (respects time + module filter, plus model filter when set)
                by_model_rows = conn.execute(
                    f"""
                    SELECT model,
                           COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                           COUNT(*) AS calls,
                           COALESCE(SUM(tokens_in), 0) AS tokens_in,
                           COALESCE(SUM(tokens_out), 0) AS tokens_out,
                           SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_calls
                    FROM usage_calls
                    {where_main}
                    GROUP BY model
                    ORDER BY cost_usd DESC, calls DESC
                    """,
                    params_main,
                ).fetchall()

                # Breakdown by module
                by_module_rows = conn.execute(
                    f"""
                    SELECT module_source,
                           COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                           COUNT(*) AS calls,
                           COALESCE(SUM(tokens_in), 0) AS tokens_in,
                           COALESCE(SUM(tokens_out), 0) AS tokens_out,
                           SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_calls
                    FROM usage_calls
                    {where_main}
                    GROUP BY module_source
                    ORDER BY cost_usd DESC, calls DESC
                    """,
                    params_main,
                ).fetchall()

                # Breakdown by call_type (storyteller, reader, embedding, search, decision, image_gen, etc.)
                by_type_rows = conn.execute(
                    f"""
                    SELECT call_type,
                           COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                           COUNT(*) AS calls,
                           COALESCE(SUM(tokens_in), 0) AS tokens_in,
                           COALESCE(SUM(tokens_out), 0) AS tokens_out
                    FROM usage_calls
                    {where_main}
                    GROUP BY call_type
                    ORDER BY cost_usd DESC, calls DESC
                    """,
                    params_main,
                ).fetchall()

        total_cost = 0.0
        total_calls = len(rows)
        priced_calls = 0
        unpriced_calls = 0
        error_calls = 0
        tokens_in_sum = 0
        tokens_out_sum = 0

        for r in rows:
            tokens_in_sum += int(r["tokens_in"] or 0)
            tokens_out_sum += int(r["tokens_out"] or 0)
            if r["status"] == "error":
                error_calls += 1
            if r["cost_usd"] is not None:
                total_cost += float(r["cost_usd"])
                priced_calls += 1
            else:
                unpriced_calls += 1

        buckets, granularity = self._build_time_buckets(
            rows=rows,
            range_key=range_key,
            since_ts=since_ts,
            now_ts=now,
            tz_offset_minutes=tz_offset_minutes,
        )

        def _mod_label(mid: str) -> str:
            return labels_map.get(mid) or mid.replace("wb_", "").replace("_", " ").title()

        return {
            "range": range_key,
            "granularity": granularity,
            "totals": {
                "cost_usd": round(total_cost, 8),
                "all_time_cost_usd": round(float(alltime_row["cost_usd"] or 0.0), 8),
                "all_time_calls": int(alltime_row["calls"] or 0),
                "prior_cost_usd": round(prior_cost_usd, 8),
                "calls": total_calls,
                "priced_calls": priced_calls,
                "unpriced_calls": unpriced_calls,
                "error_calls": error_calls,
                "tokens_in": tokens_in_sum,
                "tokens_out": tokens_out_sum,
                "avg_cost_usd": round(total_cost / priced_calls, 8) if priced_calls > 0 else 0.0,
            },
            "buckets": buckets,
            "by_model": [
                {
                    "model": r["model"] or "(unknown)",
                    "cost_usd": round(float(r["cost_usd"] or 0.0), 8),
                    "calls": int(r["calls"] or 0),
                    "tokens_in": int(r["tokens_in"] or 0),
                    "tokens_out": int(r["tokens_out"] or 0),
                    "unpriced_calls": int(r["unpriced_calls"] or 0),
                }
                for r in by_model_rows
            ],
            "by_module": [
                {
                    "module": r["module_source"] or "core",
                    "label": _mod_label(r["module_source"] or "core"),
                    "cost_usd": round(float(r["cost_usd"] or 0.0), 8),
                    "calls": int(r["calls"] or 0),
                    "tokens_in": int(r["tokens_in"] or 0),
                    "tokens_out": int(r["tokens_out"] or 0),
                    "unpriced_calls": int(r["unpriced_calls"] or 0),
                }
                for r in by_module_rows
            ],
            "by_call_type": [
                {
                    "call_type": r["call_type"] or "other",
                    "cost_usd": round(float(r["cost_usd"] or 0.0), 8),
                    "calls": int(r["calls"] or 0),
                    "tokens_in": int(r["tokens_in"] or 0),
                    "tokens_out": int(r["tokens_out"] or 0),
                }
                for r in by_type_rows
            ],
            "available_models": avail_models,
            "available_modules": [
                {"id": mid, "label": _mod_label(mid)}
                for mid in avail_mod_ids
            ],
        }

    @staticmethod
    def _build_time_buckets(
        *,
        rows: list[sqlite3.Row],
        range_key: str,
        since_ts: float | None,
        now_ts: float,
        tz_offset_minutes: int = 0,
    ) -> tuple[list[dict[str, Any]], str]:
        """Group calls into linear time buckets with both per-bucket base cost
        and running accumulated cost across the window."""
        # Client local timezone (JS getTimezoneOffset() is UTC - local in minutes;
        # we accept tz_offset_minutes as local - UTC in minutes, clamped safely).
        offset_min = max(-840, min(840, int(tz_offset_minutes or 0)))
        tz = timezone(timedelta(minutes=offset_min))

        if range_key == "24h":
            granularity = "hour"
            start_ts = since_ts if since_ts is not None else (now_ts - 24 * 3600)
        elif range_key in ("7d", "30d", "90d"):
            granularity = "day"
            start_ts = since_ts if since_ts is not None else (now_ts - 30 * 86400)
        else:
            if not rows:
                return [], "day"
            earliest = float(rows[0]["timestamp"])
            span = max(0.0, now_ts - earliest)
            if span <= 48 * 3600:
                granularity = "hour"
            elif span <= 120 * 86400:
                granularity = "day"
            else:
                granularity = "week"
            start_ts = earliest

        def _floor_dt(ts: float) -> datetime:
            dt = datetime.fromtimestamp(ts, tz=tz)
            if granularity == "hour":
                return dt.replace(minute=0, second=0, microsecond=0)
            if granularity == "day":
                return dt.replace(hour=0, minute=0, second=0, microsecond=0)
            # week (Monday-aligned)
            day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            return day_start - timedelta(days=day_start.weekday())

        step_delta = {
            "hour": timedelta(hours=1),
            "day": timedelta(days=1),
            "week": timedelta(days=7),
        }[granularity]

        first_bucket = _floor_dt(start_ts)
        last_bucket = _floor_dt(max(now_ts, float(rows[-1]["timestamp"]) if rows else now_ts))

        bucket_map: dict[int, dict[str, Any]] = {}
        cur = first_bucket
        # Cap against pathological clock skews
        max_steps = 500
        steps = 0
        while cur <= last_bucket and steps < max_steps:
            bts = int(cur.timestamp())
            if granularity == "hour":
                label = cur.strftime("%H:%M")
                tooltip_label = cur.strftime("%b %d, %H:%M")
            elif granularity == "day":
                label = cur.strftime("%b %d")
                tooltip_label = cur.strftime("%Y-%m-%d")
            else:
                label = cur.strftime("%b %d")
                tooltip_label = f"Week of {cur.strftime('%Y-%m-%d')}"
            bucket_map[bts] = {
                "bucket_start": bts,
                "label": label,
                "tooltip_label": tooltip_label,
                "base_cost_usd": 0.0,
                "accumulated_cost_usd": 0.0,
                "calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
            }
            cur = cur + step_delta
            steps += 1

        for r in rows:
            bdt = _floor_dt(float(r["timestamp"]))
            bts = int(bdt.timestamp())
            bucket = bucket_map.get(bts)
            if bucket is None:
                continue
            bucket["calls"] += 1
            bucket["tokens_in"] += int(r["tokens_in"] or 0)
            bucket["tokens_out"] += int(r["tokens_out"] or 0)
            if r["cost_usd"] is not None:
                bucket["base_cost_usd"] += float(r["cost_usd"])

        ordered = [bucket_map[k] for k in sorted(bucket_map)]
        running = 0.0
        for b in ordered:
            running += b["base_cost_usd"]
            b["base_cost_usd"] = round(b["base_cost_usd"], 8)
            b["accumulated_cost_usd"] = round(running, 8)

        return ordered, granularity
