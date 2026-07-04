import os
import re
import json
import hashlib
import asyncio
from typing import Any
from litellm import acompletion, aembedding
from backend.engine.schemas import MemorySummary, MemoryImportance
from backend.engine.llm_inspector import LLMInspector


def _llm_debug():
    return os.getenv("LLM_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


_THINK_RE = re.compile(r"^\s*<think>(.*?)</think>\s*", re.IGNORECASE | re.DOTALL)


def _split_think(content: str, reasoning: str) -> tuple[str, str]:
    """Some models emit their chain-of-thought as a separate reasoning channel
    (``delta.reasoning_content``); others wrap it in a leading ``<think>...</think>``
    block inside the normal content. When no separate reasoning was captured, pull a
    leading think-block out of the content so the narration stays clean."""
    if not reasoning and content:
        m = _THINK_RE.match(content)
        if m:
            return content[m.end():], m.group(1).strip()
    return content, reasoning


def _llm_log_req(tag, model, messages, extra=""):
    if not _llm_debug():
        return
    msg_count = len(messages)
    parts = [f"  model: {model}"]
    if msg_count <= 2:
        for m in messages:
            role = m.get("role", "?")
            content = str(m.get("content", ""))
            if len(content) > 300:
                content = content[:150] + "..." + content[-150:]
            parts.append(f"  {role}: {content}")
    else:
        parts.append(f"  messages: {msg_count}")
        for m in messages[:2]:
            role = m.get("role", "?")
            content = str(m.get("content", ""))
            if len(content) > 200:
                content = content[:200] + "..."
            parts.append(f"  {role}: {content}")
    if extra:
        parts.append(f"  {extra}")
    print(f"\n=== LLM REQ [{tag}] ===")
    for p in parts:
        print(p)
    print("=" * 60)


def _llm_log_res(tag, content, usage=None, finish=None, dim=None):
    if not _llm_debug():
        return
    parts = []
    if usage:
        parts.append(f"  tokens: {usage.get('total_tokens', '?')} (in={usage.get('prompt_tokens', '?')} out={usage.get('completion_tokens', '?')}) | finish: {finish or '?'}")
    if dim:
        parts.append(f"  dimension: {dim}")
    if content:
        c = str(content)
        if len(c) > 400:
            c = c[:200] + "\n  ...\n  " + c[-200:]
        parts.append(f"  content: {c}")
    print(f"=== LLM RES [{tag}] ===")
    for p in parts:
        print(p)
    print("=" * 60)


class LLMProviderError(RuntimeError):
    pass


class LLMService:
    def __init__(self, mode: str = None):
        self.mode = (mode or os.getenv("LLM_MODE", "live")).strip().lower()
        self.storyteller_model = "gemini/gemini-2.5-flash"
        self.storyteller_fallback_models = []
        self.reader_model = "gemini/gemini-2.5-flash"
        self.embedding_model = "gemini/gemini-embedding-001"
        self.module_fast_model = "gemini/gemini-2.5-flash"
        self.provider_retry_attempts = 2
        self.provider_retry_delay_seconds = 1.0
        #: Maps an (effective) model string -> OpenRouter upstream provider to
        #: pin via the request-body ``provider`` routing param. Empty for
        #: non-OpenRouter providers.
        self._or_provider_routes: dict[str, str] = {}
        self._temperature = None
        self._top_p = None
        self._max_output_tokens = None
        self.inspector: LLMInspector | None = None

    def set_inspector(self, inspector: LLMInspector):
        self.inspector = inspector

    def reconfigure(self, provider_id: str, config: dict):
        print(f"[LLMService] reconfigure called: provider='{provider_id}'")
        model_keys = ["storyteller_model", "reader_model", "embedding_model", "module_fast_model", "storyteller_fallback_models"]
        for k in model_keys:
            print(f"[LLMService] reconfigure config: {k}='{config.get(k, '<MISSING>')}'")
        mapping = {
            "storyteller_model": "storyteller_model",
            "storyteller_fallback_models": "storyteller_fallback_models",
            "reader_model": "reader_model",
            "embedding_model": "embedding_model",
            "module_fast_model": "module_fast_model",
            "retry_attempts": "provider_retry_attempts",
            "retry_delay_seconds": "provider_retry_delay_seconds",
            "temperature": "_temperature",
            "top_p": "_top_p",
            "max_output_tokens": "_max_output_tokens",
        }
        for config_key, attr in mapping.items():
            val = config.get(config_key)
            if val is not None and val != "":
                if attr == "storyteller_fallback_models":
                    if isinstance(val, str):
                        val = [m.strip() for m in val.split(",") if m.strip()]
                elif attr == "provider_retry_attempts":
                    val = max(1, int(val))
                elif attr == "provider_retry_delay_seconds":
                    val = max(0.0, float(val))
                elif attr in ("_temperature", "_top_p"):
                    val = float(val)
                elif attr == "_max_output_tokens":
                    val = int(val)
                setattr(self, attr, val)

        # Slots the provider config left empty must never keep another
        # provider's leftover/default model — degrade to this provider's
        # reader model instead so every call stays on the configured provider.
        if not config.get("module_fast_model"):
            self.module_fast_model = self.reader_model
        if not config.get("reader_model"):
            self.reader_model = self.storyteller_model
            if not config.get("module_fast_model"):
                self.module_fast_model = self.storyteller_model

        # Build per-slot OpenRouter provider routing. A per-slot provider wins;
        # otherwise the singular "Default Provider" applies. Keyed by the same
        # effective model string later passed to acompletion/aembedding.
        self._or_provider_routes = {}
        if provider_id == "openrouter":
            default_prov = config.get("openrouter_provider") or ""
            slot_pairs = [
                (self.storyteller_model, config.get("openrouter_storyteller_provider")),
                (self.reader_model, config.get("openrouter_reader_provider")),
                (self.embedding_model, config.get("openrouter_embedding_provider")),
                (self.module_fast_model, config.get("openrouter_fast_provider")),
            ]
            for model_str, prov in slot_pairs:
                route = (prov or default_prov).strip() if isinstance(prov or default_prov, str) else ""
                if model_str and route:
                    self._or_provider_routes[model_str] = route
            fb_prov = (config.get("openrouter_fallback_provider") or default_prov)
            fb_route = fb_prov.strip() if isinstance(fb_prov, str) else ""
            if fb_route:
                for model_str in self.storyteller_fallback_models:
                    if model_str:
                        self._or_provider_routes[model_str] = fb_route
            if self._or_provider_routes:
                print(f"[LLMService] OpenRouter provider routes: {self._or_provider_routes}")

        print(f"[LLMService] Reconfigured for provider '{provider_id}': storyteller={self.storyteller_model}, reader={self.reader_model}, embedding={self.embedding_model}, module_fast={self.module_fast_model}")

    def _provider_route_kwargs(self, model: str) -> dict:
        """Return litellm kwargs that pin the OpenRouter upstream provider for
        ``model`` via the request-body ``provider`` routing param, or ``{}``."""
        prov = self._or_provider_routes.get(model)
        if not prov:
            return {}
        return {"extra_body": {"provider": {"order": [prov]}}}

    async def simple_completion(self, messages: list[dict[str, str]], model: str = None, max_tokens: int = None, temperature: float = None, top_p: float = None, response_format: Any = None, inspector_ctx: dict = None, return_reasoning: bool = False, return_usage: bool = False):
        # NOTE: `max_tokens` should NOT be used for content generation. It cuts off LLM output
        # mid-sentence and causes bugs. Prefer prompt-level output control instead
        # (e.g. "respond ONLY with valid JSON", "keep to one sentence", "3 paragraphs of prose").
        # This parameter remains for backward compatibility but callers should avoid passing it.
        model = model or self.reader_model
        kwargs = {"model": model, "messages": messages}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if response_format is not None:
            kwargs["response_format"] = response_format
        kwargs.update(self._provider_route_kwargs(model))
        extra_parts = []
        if max_tokens is not None:
            extra_parts.append(f"max_tokens: {max_tokens}")
        if temperature is not None:
            extra_parts.append(f"temp: {temperature}")
        if top_p is not None:
            extra_parts.append(f"top_p: {top_p}")
        extra = " | ".join(extra_parts) if extra_parts else ""
        _llm_log_req("simple_completion", model, messages, extra)

        ctx = inspector_ctx or {}
        cid = None
        if self.inspector:
            cid = await self.inspector.start_call(
                call_type=ctx.get("call_type", "reader"),
                model=model,
                step=ctx.get("step", "simple_completion"),
                module_source=ctx.get("module_source", ""),
                input_data=messages,
            )

        try:
            response = await acompletion(**kwargs)
            message = response.choices[0].message
            content = message.content or ""
            reasoning = getattr(message, 'reasoning_content', '') or ''
            usage = response.usage.to_dict() if hasattr(response, 'usage') else {}
            _llm_log_res("simple_completion", content, usage, response.choices[0].finish_reason)

            if self.inspector and cid:
                await self.inspector.end_call(cid, messages, content,
                                        usage.get("prompt_tokens", 0),
                                        usage.get("completion_tokens", 0))
            if return_usage:
                return content, reasoning, usage
            return (content, reasoning) if return_reasoning else content
        except asyncio.CancelledError:
            # CancelledError is a BaseException, so the handler below never
            # sees it — without this the inspector record stays "running"
            # forever after the user stops a turn.
            if self.inspector and cid:
                await self.inspector.end_call(cid, messages, cancelled=True)
            raise
        except Exception as e:
            if self.inspector and cid:
                await self.inspector.end_call(cid, messages, "", error=str(e))
            raise

    async def get_embedding(self, text: str, inspector_ctx: dict = None) -> list[float]:
        ctx = inspector_ctx or {}
        cid = None
        if self.inspector:
            cid = await self.inspector.start_call(
                call_type=ctx.get("call_type", "embedding"),
                model=self.embedding_model,
                step=ctx.get("step", "get_embedding"),
                module_source=ctx.get("module_source", ""),
                input_data=text,
            )

        if self.mode == "mock":
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            result = [round((byte / 255.0) * 2 - 1, 6) for byte in digest[:16]]
            if self.inspector and cid:
                await self.inspector.end_call(cid, text, f"mock vector [{len(result)}]", 0, 0)
            return result

        if _llm_debug():
            t = text if len(text) <= 200 else text[:200] + "..."
            print(f"\n=== LLM EMB [{self.embedding_model}] ===")
            print(f"  input: {t}")
            print("=" * 60)

        try:
            emb_kwargs = {"model": self.embedding_model, "input": text}
            emb_kwargs.update(self._provider_route_kwargs(self.embedding_model))
            response = await aembedding(**emb_kwargs)
            embedding = response.data[0]["embedding"]
            if _llm_debug():
                print(f"=== LLM EMB RES ===")
                print(f"  dimension: {len(embedding)}")
                print("=" * 60)

            if self.inspector and cid:
                await self.inspector.end_call(cid, text, f"vector dim={len(embedding)}",
                                        tokens_in=0, tokens_out=0)
            return embedding
        except asyncio.CancelledError:
            if self.inspector and cid:
                await self.inspector.end_call(cid, text, cancelled=True)
            raise
        except Exception as e:
            if self.inspector and cid:
                await self.inspector.end_call(cid, text, "", error=str(e))
            raise

    async def generate_story(self, prompt: str, streaming_callback=None) -> str:
        messages = [{"role": "system", "content": "You are a creative storyteller in a text-based RPG."},
                    {"role": "user", "content": prompt}]
        result = await self.generate_story_from_messages(messages, streaming_callback)
        return result["content"]

    async def generate_story_from_messages(self, messages: list[dict[str, str]], streaming_callback=None, inspector_ctx: dict = None, reasoning_callback=None) -> dict[str, str]:
        """Generate storyteller narration. Returns ``{"content", "reasoning", "model", "usage"}``
        where ``reasoning`` is the model's thinking (separate channel or a <think> block,
        empty when the model produced none), ``model`` is the model that answered (after
        fallbacks), and ``usage`` is the provider's token usage dict ({} when unreported)."""
        ctx = inspector_ctx or {}
        if self.mode == "mock":
            story = self._mock_story_from_messages(messages)
            reasoning = self._mock_reasoning(messages)
            if self.inspector:
                cid = await self.inspector.start_call(call_type=ctx.get("call_type", "storyteller"), model="mock", step=ctx.get("step", "storyteller"), module_source=ctx.get("module_source", ""), streaming=bool(streaming_callback), input_data=messages)
                await self.inspector.end_call(cid, messages, story, 0, 0)
            if reasoning_callback and reasoning:
                await reasoning_callback(reasoning)
            if streaming_callback:
                for token in story.split(" "):
                    await streaming_callback(token + " ")
            return {"content": story, "reasoning": reasoning, "model": "mock", "usage": {}}

        return await self._complete_story_with_fallbacks(messages, streaming_callback, inspector_ctx, reasoning_callback)

    async def extract_mutations(self, story_text: str, schema: dict, inspector_ctx: dict = None) -> dict:
        ctx = inspector_ctx or {}
        if self.mode == "mock":
            result = self._mock_mutations(schema)
            if self.inspector:
                cid = await self.inspector.start_call(call_type=ctx.get("call_type", "reader"), model="mock", step=ctx.get("step", "reader"), module_source=ctx.get("module_source", ""), input_data=story_text)
                await self.inspector.end_call(cid, story_text, str(result), 0, 0)
            return result

        ctx = inspector_ctx or {}
        schema_str = json.dumps(schema, indent=2)
        prompt = f"""
Given the following story text, extract any game state mutations into a strict JSON format matching the schema provided. 
If there are no mutations, return an empty JSON object {{}}.

Schema:
{schema_str}

Story Text:
{story_text}

Respond ONLY with valid JSON. Do not include markdown formatting like ```json.
"""
        messages = [{"role": "system", "content": "You are a data extraction AI that strictly outputs JSON."},
                    {"role": "user", "content": prompt}]

        for attempt in range(2):
            try:
                content = await self.simple_completion(
                    messages=messages,
                    model=self.reader_model,
                    response_format={"type": "json_object"},
                    inspector_ctx={**ctx, "call_type": ctx.get("call_type", "reader"), "step": ctx.get("step", "reader")},
                )
                return self._parse_json_object(content)
            except Exception as e:
                print(f"Failed to parse Reader JSON on attempt {attempt + 1}: {e}")

        return {}

    async def summarize_memory(self, text: str) -> str:
        if self.mode == "mock":
            first_line = text.strip().splitlines()[0] if text.strip() else "Nothing notable happened."
            return first_line[:240]

        prompt = f"Extract a 1-sentence factual summary of the most important event from this text:\n\n{text}"
        return await self.generate_story(prompt)

    async def summarize_memory_structured(self, text: str, turn_range: str = "", inspector_ctx: dict = None) -> MemorySummary:
        ctx = inspector_ctx or {}
        if self.mode == "mock":
            result = self._mock_memory_summary(text, turn_range)
            if self.inspector:
                cid = await self.inspector.start_call(call_type=ctx.get("call_type", "librarian"), model="mock", step=ctx.get("step", "librarian:summary"), module_source=ctx.get("module_source", ""), input_data=text)
                await self.inspector.end_call(cid, text, result.summary, 0, 0)
            return result

        prompt = f"""Analyze this chunk of RPG narrative history and produce a structured summary.

Turn range: {turn_range}

Narrative text:
{text}

Return a JSON object with:
- "summary": a one-paragraph factual summary of the most important events
- "entities": a list of named entities mentioned (characters, places, items, factions)
- "topics": a list of key topics or themes (e.g. combat, diplomacy, exploration, mystery, trade)
- "turn_range": the range of turns covered (use "{turn_range}" or derive from context)"""

        messages = [{"role": "system", "content": "You are a meticulous lore archivist. Extract structured memory summaries from RPG narratives."},
                    {"role": "user", "content": prompt}]

        try:
            content = await self.simple_completion(
                messages=messages,
                model=self.reader_model,
                response_format=MemorySummary,
                inspector_ctx={**ctx, "call_type": ctx.get("call_type", "librarian"), "step": ctx.get("step", "librarian:summary")},
            )
            parsed = self._parse_json_object(content)
            return MemorySummary(**parsed)
        except Exception as e:
            print(f"Structured memory summary failed, falling back to legacy summary: {e}")
            legacy = await self.summarize_memory(text)
            return MemorySummary(summary=legacy, entities=[], topics=[], turn_range=turn_range)

    async def score_memory_importance(self, summary: str) -> int:
        if self.mode == "mock":
            return 5

        prompt = f"Score this RPG memory from 1 to 10 for future narrative importance. Respond only with an integer.\n\n{summary}"
        try:
            content = await self.generate_story(prompt)
            return max(1, min(10, int(content.strip())))
        except Exception as e:
            print(f"Failed to score memory importance: {e}")
            return 5

    async def score_memory_importance_structured(self, summary: str, entities: list[str] = None, topics: list[str] = None, inspector_ctx: dict = None) -> MemoryImportance:
        ctx = inspector_ctx or {}
        if self.mode == "mock":
            result = self._mock_memory_importance(summary)
            if self.inspector:
                cid = await self.inspector.start_call(call_type=ctx.get("call_type", "librarian"), model="mock", step=ctx.get("step", "librarian:importance"), module_source=ctx.get("module_source", ""), input_data=summary)
                await self.inspector.end_call(cid, summary, f"importance={result.importance} permanent={result.permanent}", 0, 0)
            return result

        context = ""
        if entities:
            context += f"Entities involved: {', '.join(entities)}\n"
        if topics:
            context += f"Topics: {', '.join(topics)}\n"

        prompt = f"""Evaluate the narrative importance of this RPG memory.

{context}
Memory summary: {summary}

Score from 1 (trivial background detail) to 10 (campaign-defining event).
Set "permanent" to true only for truly world-altering events (major character death, kingdom falls, artifact discovered).
Return a JSON object with:
- "importance": integer 1-10
- "reason": single-sentence justification
- "permanent": boolean"""

        messages = [{"role": "system", "content": "You are a story editor evaluating which narrative events matter for future storytelling."},
                    {"role": "user", "content": prompt}]

        try:
            content = await self.simple_completion(
                messages=messages,
                model=self.reader_model,
                response_format=MemoryImportance,
                inspector_ctx={**ctx, "call_type": ctx.get("call_type", "librarian"), "step": ctx.get("step", "librarian:importance")},
            )
            parsed = self._parse_json_object(content)
            return MemoryImportance(**parsed)
        except Exception as e:
            print(f"Structured importance scoring failed, falling back to legacy scoring: {e}")
            legacy_score = await self.score_memory_importance(summary)
            return MemoryImportance(importance=legacy_score, reason="Fallback score", permanent=False)

    def _parse_json_object(self, content: str) -> dict:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("Reader response must be a JSON object.")
        return parsed

    async def _complete_story_with_fallbacks(self, messages: list[dict[str, str]], streaming_callback=None, inspector_ctx: dict = None, reasoning_callback=None) -> dict[str, str]:
        errors = []
        models = [self.storyteller_model] + [model for model in self.storyteller_fallback_models if model != self.storyteller_model]
        # Only stream on the very first attempt. If it fails (even after partial tokens
        # were already sent to the client), retry without streaming to avoid sending a
        # second response stream on top of the first.
        streaming_used = False

        for model in models:
            for attempt in range(self.provider_retry_attempts):
                cb = streaming_callback if not streaming_used else None
                rcb = reasoning_callback if not streaming_used else None
                try:
                    return await self._complete_story_once(model, messages, cb, inspector_ctx, rcb)
                except Exception as exc:
                    if cb:
                        streaming_used = True
                    errors.append(f"{model} attempt {attempt + 1}: {exc}")
                    print(f"Storyteller provider call failed for {model} on attempt {attempt + 1}: {exc}")
                    if attempt + 1 < self.provider_retry_attempts and self.provider_retry_delay_seconds:
                        await asyncio.sleep(self.provider_retry_delay_seconds)

        raise LLMProviderError("Storyteller provider unavailable. " + " | ".join(errors[-3:]))

    async def _complete_story_once(self, model: str, messages: list[dict[str, str]], streaming_callback=None, inspector_ctx: dict = None, reasoning_callback=None) -> dict[str, str]:
        kwargs = {"model": model, "messages": messages}
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if self._top_p is not None:
            kwargs["top_p"] = self._top_p
        # `_max_output_tokens` is a user-facing config for storyteller output length.
        # Acceptable here since storyteller generates unbounded narrative prose.
        # For module/agent LLM calls, prefer prompt-level output control instead of max_tokens.
        if self._max_output_tokens is not None:
            kwargs["max_tokens"] = self._max_output_tokens

        ctx = inspector_ctx or {}
        cid = None
        if self.inspector:
            cid = await self.inspector.start_call(
                call_type=ctx.get("call_type", "storyteller"),
                model=model,
                step=ctx.get("step", "storyteller"),
                module_source=ctx.get("module_source", ""),
                streaming=bool(streaming_callback),
                input_data=messages,
            )

        try:
            if streaming_callback:
                kwargs["stream"] = True
                kwargs.update(self._provider_route_kwargs(model))
                extra = f"stream: True | max_tokens: {self._max_output_tokens}" if self._max_output_tokens is not None else "stream: True"
                _llm_log_req("storyteller_stream", model, messages, extra)
                response = await acompletion(**kwargs)
                full_text = ""
                full_reasoning = ""
                usage = {}
                async for chunk in response:
                    # Some providers attach token usage to the final chunk.
                    chunk_usage = getattr(chunk, 'usage', None)
                    if chunk_usage:
                        usage = chunk_usage.to_dict() if hasattr(chunk_usage, 'to_dict') else dict(chunk_usage)
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content = getattr(delta, 'content', '')
                    reasoning = getattr(delta, 'reasoning_content', '') or ''
                    if reasoning:
                        full_reasoning += reasoning
                        if reasoning_callback:
                            await reasoning_callback(reasoning)
                    if content:
                        full_text += content
                        await streaming_callback(content)
                full_text, full_reasoning = _split_think(full_text, full_reasoning)
                _llm_log_res("storyteller_stream", full_text, usage or None)
                if self.inspector and cid:
                    await self.inspector.end_call(cid, messages, full_text,
                                            usage.get("prompt_tokens", 0),
                                            usage.get("completion_tokens", 0))
                return {"content": full_text, "reasoning": full_reasoning, "model": model, "usage": usage}

            # No inspector_ctx here: this call is already recorded above as the
            # storyteller call, so passing ctx would create a duplicate record.
            result, reasoning, usage = await self.simple_completion(
                messages=messages,
                model=model,
                temperature=self._temperature,
                top_p=self._top_p,
                max_tokens=self._max_output_tokens,
                return_usage=True,
            )
            result, reasoning = _split_think(result, reasoning)
            if reasoning_callback and reasoning:
                await reasoning_callback(reasoning)
            if self.inspector and cid:
                await self.inspector.end_call(cid, messages, result,
                                        usage.get("prompt_tokens", 0),
                                        usage.get("completion_tokens", 0))
            return {"content": result, "reasoning": reasoning, "model": model, "usage": usage}
        except asyncio.CancelledError:
            if self.inspector and cid:
                await self.inspector.end_call(cid, messages, cancelled=True)
            raise
        except Exception as e:
            if self.inspector and cid:
                await self.inspector.end_call(cid, messages, "", error=str(e))
            raise

    def _mock_story_from_messages(self, messages: list[dict[str, str]]) -> str:
        player_action = "I look around."
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            player_action = message.get("content", "").strip() or player_action
            break

        marker = "Player Action:"
        if marker in player_action:
            player_action = player_action.split(marker, 1)[1].strip().splitlines()[0].strip() or player_action
        return f"Mock outcome: {player_action} resolves without major lasting consequences."

    def _mock_reasoning(self, messages: list[dict[str, str]]) -> str:
        """Deterministic fake chain-of-thought so the collapsible thinking UI can be
        exercised in mock mode without a reasoning-capable provider."""
        player_action = "the player's action"
        for message in reversed(messages):
            if message.get("role") == "user":
                player_action = (message.get("content", "").strip() or player_action)[:80]
                break
        return (
            f"(mock reasoning) The player attempts: {player_action}. "
            "Weighing plausible outcomes, narrative stakes, and continuity before narrating a measured result."
        )

    def _mock_mutations(self, schema: dict) -> dict:
        mutations = {}
        for module_id, module_schema in schema.items():
            if not isinstance(module_schema, dict):
                continue
            module_mutations = {}
            for field_name in module_schema:
                if field_name.endswith("_change"):
                    module_mutations[field_name] = 0
            if module_mutations:
                mutations[module_id] = module_mutations
        return mutations

    def _mock_memory_summary(self, text: str, turn_range: str) -> MemorySummary:
        first_line = text.strip().splitlines()[0] if text.strip() else "Nothing notable happened."
        summary = first_line[:240]
        return MemorySummary(
            summary=summary,
            entities=[],
            topics=["general"],
            turn_range=turn_range or "",
        )

    def _mock_memory_importance(self, summary: str) -> MemoryImportance:
        return MemoryImportance(importance=5, reason="Mock deterministic score", permanent=False)
