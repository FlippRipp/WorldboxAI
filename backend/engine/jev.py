"""Optional typed decisions. Never a replacement for the text-model slots."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import tempfile
import time

import httpx


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/systemone"
NANOGPT_ENDPOINT = "https://api.nano-gpt.com/api/v1/systemone"
PRICE_DATE = "2026-10-01"
INPUT_USD_PER_MILLION = 0.042
FALLBACK_CONTEXT = ContextVar("jev_fallback_context", default=None)
DEFAULTS = {"enabled": False, "rpg_enabled": True, "npc_enabled": True,
            "plot_enabled": True, "tts_enabled": True, "model": "jev-latest", "api_key": ""}


class Uncertain(ValueError):
    """A valid decision could not be accepted; run the existing model path."""


def choice(instructions, options):
    return {"type": "choice", "instructions": instructions, "criteria": options}


def noul(instructions):
    return {"type": "noul", "instructions": instructions}


def _probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Uncertain("invalid probability")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise Uncertain("invalid probability")
    return value


@dataclass
class Decision:
    answers: dict = field(default_factory=dict)
    reason: str = "unavailable"
    call_id: str = ""
    inspector: object = None
    model: str = ""
    payload: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    module: str = ""
    step: str = ""
    finished: bool = False
    outcome: str = ""
    duration_ms: int | None = None

    def pick(self, key):
        answer = self.answers.get(key, {})
        if answer.get("type") != "choice" or answer.get("confidence", 0) < 0.90:
            raise Uncertain(self.reason or f"uncertain choice: {key}")
        return answer["choice"]

    def choice_distribution(self, key):
        answer = self.answers.get(key, {})
        if answer.get("type") != "choice":
            raise Uncertain(self.reason or f"uncertain choice: {key}")
        selected = answer.get("choice")
        probs = answer.get("probabilities")
        if not isinstance(selected, str) or not isinstance(probs, dict):
            raise Uncertain(self.reason or f"uncertain choice: {key}")
        return selected, probs, answer.get("confidence", 0)

    def pick_supported(self, key, unsupported=("unsupported", "outside"), min_choice_prob=0.50):
        selected, probs, conf = self.choice_distribution(key)
        if selected in unsupported:
            return selected
        if conf >= 0.90:
            return selected
        supported_mass = 1.0 - sum(probs.get(u, 0.0) for u in unsupported)
        if supported_mass < 0.90 or probs.get(selected, 0.0) < min_choice_prob:
            raise Uncertain(self.reason or f"uncertain choice: {key}")
        return selected

    def pick_ordinal(self, key, ordered_keys, radius=1, forbidden=()):
        selected, probs, conf = self.choice_distribution(key)
        if selected not in ordered_keys or selected in forbidden:
            raise Uncertain(self.reason or f"uncertain choice: {key}")
        if conf >= 0.90:
            return selected
        if sum(probs.get(f, 0.0) for f in forbidden) > 0.10:
            raise Uncertain(self.reason or f"uncertain choice: {key}")
        idx = ordered_keys.index(selected)
        band = ordered_keys[max(0, idx - radius):idx + radius + 1]
        if sum(probs.get(k, 0.0) for k in band) < 0.90 or probs.get(selected, 0.0) < 0.30:
            raise Uncertain(self.reason or f"uncertain choice: {key}")
        return selected

    def yes(self, key, high=0.90, low=0.10):
        answer = self.answers.get(key, {})
        if answer.get("type") != "noul":
            raise Uncertain(self.reason or f"missing decision: {key}")
        probability = answer["noul"]
        if probability >= high:
            return True
        if probability <= low:
            return False
        raise Uncertain(f"uncertain binary decision: {key}")

    def no_work(self, key):
        answer = self.answers.get(key, {})
        return answer.get("type") == "noul" and answer["noul"] <= 0.02

    def _estimated_cost_usd(self):
        cost = self.usage.get("cost")
        if not isinstance(cost, bool) and isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0:
            return float(cost)
        if "input_tokens" in self.usage:
            return self.usage["input_tokens"] * INPUT_USD_PER_MILLION / 1_000_000
        return None

    async def finish(self, outcome, reason=""):
        if self.finished:
            return
        self.finished = True
        self.outcome = outcome
        self.reason = reason or self.reason
        if self.inspector and self.call_id:
            est_cost = self._estimated_cost_usd()
            raw_cost = self.usage.get("cost")
            has_reported = (
                not isinstance(raw_cost, bool)
                and isinstance(raw_cost, (int, float))
                and math.isfinite(raw_cost)
                and raw_cost >= 0
            )
            cost_source = "reported" if has_reported else ("priced" if est_cost is not None else "unknown")
            await self.inspector.end_call(
                self.call_id, output_data=json.dumps(self.payload, ensure_ascii=False),
                tokens_in=self.usage.get("input_tokens", 0),
                tokens_out=self.usage.get("output_tokens", 0),
                cancelled=outcome == "cancelled",
                metadata={"model": self.model, "decision_outcome": outcome,
                          "fallback_reason": self.reason if outcome == "fallback" else "",
                          "estimated_cost_usd": est_cost,
                          "pricing_date": PRICE_DATE,
                          "provider": "jev",
                          "cost_source": cost_source,
                          **({"duration_ms": self.duration_ms} if self.duration_ms is not None else {})})


def _normalize_model(model: str, is_openrouter: bool) -> str:
    clean = (model or "jev-latest").strip()
    for prefix in ("openrouter/", "nano-gpt/", "nanogpt/"):
        if clean.startswith(prefix):
            clean = clean.removeprefix(prefix).strip()
            break
    if is_openrouter:
        if clean == "typesafe/jev-latest":
            return "jev-latest"
        return clean or "jev-latest"
    if clean.startswith("~typesafe/"):
        return clean.removeprefix("~typesafe/") or "jev-latest"
    if clean.startswith("typesafe/"):
        return clean.removeprefix("typesafe/") or "jev-latest"
    return clean or "jev-latest"


class JevService:
    def __init__(self, path="data/providers/jev/config.json", transport=None):
        self.path = Path(path)
        self.transport = transport
        self.timeout = 3.0

    def _config(self):
        config = dict(DEFAULTS)
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update({k: v for k, v in saved.items()
                               if k in DEFAULTS and type(v) is type(DEFAULTS[k])})
        except (OSError, ValueError):
            pass
        return config

    def _router_connection(self) -> tuple[str, str]:
        if self.path.parent.name != "jev":
            return "", ""
        providers_dir = self.path.parent.parent
        active_path = providers_dir / "active_provider.json"
        try:
            active_data = json.loads(active_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "", ""
        if not isinstance(active_data, dict):
            return "", ""
        active = active_data.get("active")
        routes = {
            "openrouter": (OPENROUTER_ENDPOINT, "OPENROUTER_API_KEY"),
            "nanogpt": (NANOGPT_ENDPOINT, "NANOGPT_API_KEY"),
        }
        if active not in routes:
            return "", ""
        endpoint, env_var = routes[active]
        cfg_path = providers_dir / active / "config.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and isinstance(cfg.get("api_key"), str):
                key = cfg["api_key"].strip()
                if key:
                    return endpoint, key
        except (OSError, ValueError):
            pass
        if not cfg_path.exists() and providers_dir == Path("data/providers"):
            key = os.getenv(env_var, "").strip()
            if key:
                return endpoint, key
        return "", ""

    def _connection(self, config=None):
        config = config if config is not None else self._config()
        if config.get("api_key"):
            return ENDPOINT, config["api_key"], False
        router_endpoint, router_key = self._router_connection()
        if router_key:
            return router_endpoint, router_key, True
        return ENDPOINT, "", False

    def has_credentials(self) -> bool:
        return bool(self._connection()[1])

    def public_config(self):
        config = self._config()
        config["api_key_set"] = bool(config.pop("api_key"))
        return config

    def save(self, updates):
        allowed = set(DEFAULTS) | {"remove_api_key"}
        if not isinstance(updates, dict) or set(updates) - allowed:
            raise ValueError("Unknown Jev setting")
        config = self._config()
        for key, value in updates.items():
            if key in ("enabled", "rpg_enabled", "npc_enabled", "plot_enabled", "tts_enabled", "remove_api_key"):
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be a boolean")
            elif not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            if key in DEFAULTS:
                config[key] = value.strip() if isinstance(value, str) else value
        if not config["model"]:
            raise ValueError("A Jev model is required")
        if updates.get("remove_api_key"):
            config["api_key"] = ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replacement prevents a settings save from exposing a partial config.
        fd, name = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(config, out, indent=2)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return self.public_config()

    async def decide(self, state, questions, *, module, step, mode="live", inspector=None, test=False):
        result = Decision(module=module, step=step, inspector=inspector)
        config = self._config()
        slot = {
            "wb_core_rpg": "rpg_enabled",
            "wb_npc_system": "npc_enabled",
            "wb_plot_director": "plot_enabled",
            "wb_tts": "tts_enabled",
        }.get(module)
        if not test:
            if mode == "mock" or not config["enabled"] or not module or (slot and not config[slot]):
                result.reason = "disabled or unavailable"
                return result
        endpoint, api_key, is_openrouter = self._connection(config)
        if not api_key:
            result.reason = "missing API key"
            return result
        request_model = _normalize_model(config["model"], is_openrouter)
        result.model = request_model
        request = {"state": state, "questions": questions, "model": request_model}
        if inspector:
            result.call_id = await inspector.start_call(call_type="decision", model=result.model,
                step=step, module_source=module, input_data=request)
        started = time.monotonic()
        try:
            if not questions or not isinstance(questions, dict):
                raise Uncertain("empty questions")
            for question in questions.values():
                if question.get("type") == "choice":
                    options = question.get("criteria")
                    if not isinstance(options, dict) or not 2 <= len(options) <= 255:
                        raise Uncertain("unsupported option count")
                elif question.get("type") != "noul":
                    raise Uncertain("unsupported question type")
            async def send():
                async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
                    response = await client.post(endpoint, json=request,
                        headers={"Authorization": f"Bearer {api_key}"})
                    response.raise_for_status()
                    return response.json()
            payload = await asyncio.wait_for(send(), timeout=self.timeout)
            result.duration_ms = max(1, int((time.monotonic() - started) * 1000))
            # Retain billable usage even if an answer subsequently fails validation.
            if isinstance(payload, dict):
                usage = payload.get("usage", {})
                if isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                        for k in ("input_tokens", "output_tokens")):
                    result.usage = usage
                if isinstance(payload.get("model"), str) and payload["model"]:
                    result.model = payload["model"]
            answers = payload.get("answers")
            if not isinstance(answers, dict) or set(answers) != set(questions):
                raise Uncertain("missing or unexpected answers")
            for key, question in questions.items():
                answer = answers[key]
                if not isinstance(answer, dict) or answer.get("type") != question["type"]:
                    raise Uncertain("answer type mismatch")
                if question["type"] == "noul":
                    _probability(answer.get("noul"))
                else:
                    options = question["criteria"]
                    probs = answer.get("probabilities", {})
                    if not isinstance(probs, dict) or set(probs) != set(options) or answer.get("choice") not in options:
                        raise Uncertain("invalid choice options")
                    _probability(answer.get("confidence"))
                    if abs(sum(_probability(p) for p in probs.values()) - 1) > 0.001:
                        raise Uncertain("invalid probability distribution")
                    if probs[answer["choice"]] < max(probs.values()):
                        raise Uncertain("choice disagrees with distribution")
            usage = payload.get("usage", {})
            if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                    for k in ("input_tokens", "output_tokens")):
                raise Uncertain("invalid usage")
            if not isinstance(payload.get("model"), str) or not payload["model"]:
                raise Uncertain("missing model version")
            result.answers, result.payload, result.usage = answers, payload, usage
            result.model, result.reason = payload["model"], ""
        except asyncio.CancelledError:
            await result.finish("cancelled")
            raise
        except Exception as exc:
            # Never log response bodies or exception messages that may echo credentials.
            if isinstance(exc, httpx.HTTPStatusError):
                result.reason = f"HTTP {exc.response.status_code}"
            elif isinstance(exc, Uncertain):
                result.reason = str(exc)
            else:
                result.reason = type(exc).__name__
            await result.finish("fallback")
        return result

    async def test_connection(self):
        result = await self.decide("Connection check.", {"ok": noul("Is this a connection check?")},
                                   module="diagnostic", step="jev:test", test=True)
        await result.finish("accepted" if result.answers else "fallback")
        return {"success": bool(result.answers), "model": result.model,
                "message": "Connected to Jev." if result.answers else result.reason}


async def ask(sdk, state, questions, module, step):
    """Support older module SDK stubs while keeping unavailable distinct from no."""
    decide = getattr(sdk.llm, "decide", None)
    if not decide:
        return Decision(module=module, step=step)
    return await decide(state, questions, module=module, step=step)


async def fallback_generate(sdk, decision, prompt, preference):
    await decision.finish("fallback", decision.reason or "uncertain or unsupported decision")
    context = {"decision_parent_id": decision.call_id, "module_source": decision.module,
               "step": decision.step + (":generation" if decision.outcome == "accepted" else ":fallback")} if decision.call_id else None
    token = FALLBACK_CONTEXT.set(context)
    try:
        return await sdk.llm.generate(prompt, model_preference=preference)
    finally:
        FALLBACK_CONTEXT.reset(token)
