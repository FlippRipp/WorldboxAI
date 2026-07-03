"""LLM access bridge exposed to modules through the WorldBox SDK."""
import os
import logging

logger = logging.getLogger(__name__)


class LLMBridge:
    """Modules never name models — they ask for a preference slot
    ("fastest"/"balanced"/"smartest") which is resolved against the live
    LLMService at call time, so provider reconfiguration mid-session is
    always respected. Env vars are a last resort for service-less use
    (tests); there are no hardcoded model fallbacks."""

    def __init__(self):
        self._service = None
        self._inspector = None
        self._current_module = ""

    def _set_service(self, service):
        self._service = service
        if hasattr(service, 'inspector') and service.inspector:
            self._inspector = service.inspector

    @property
    def _mode(self) -> str:
        if self._service is not None:
            return self._service.mode
        return os.getenv("LLM_MODE", "live").strip().lower()

    @property
    def _storyteller_model(self) -> str:
        if self._service is not None:
            return self._service.storyteller_model
        return os.getenv("STORYTELLER_MODEL", "")

    @property
    def _reader_model(self) -> str:
        if self._service is not None:
            return self._service.reader_model
        return os.getenv("READER_MODEL", "")

    @property
    def _fast_model(self) -> str:
        env = os.getenv("MODULE_FAST_MODEL", "")
        if env:
            return env
        if self._service is not None:
            return getattr(self._service, "module_fast_model", "") or self._service.reader_model
        return self._reader_model

    async def generate(self, prompt: str, model_preference: str = "balanced", max_tokens: int = None) -> str:
        # NOTE: `max_tokens` should NOT be used for content generation. It cuts off LLM output
        # mid-sentence and causes bugs. Instead, prompt the model to produce the desired output
        # length (e.g. "respond with ONLY valid JSON", "keep to one sentence", "2-3 paragraphs").
        # This parameter remains for backward compatibility but callers should not pass it.
        mod_src = self._current_module or "module"
        model = self._pick_model(model_preference) if self._service else self._reader_model

        if self._mode == "mock":
            result = f"[mock llm response for: {prompt[:80]}...]"
            if self._inspector:
                cid = await self._inspector.start_call(call_type="module_fast", model="mock", step="module:generate", module_source=mod_src, input_data=prompt)
                await self._inspector.end_call(cid, prompt, result, 0, 0)
            return result

        if not model:
            logger.error("Module LLM call skipped: no LLM service configured and no model env override set.")
            return ""

        messages = [{"role": "user", "content": prompt}]

        try:
            if self._service is not None:
                return await self._service.simple_completion(
                    messages=messages,
                    model=model,
                    max_tokens=max_tokens,
                    inspector_ctx={"call_type": "module_fast", "step": "module:generate", "module_source": mod_src},
                )
            cid = None
            if self._inspector:
                cid = await self._inspector.start_call(call_type="module_fast", model=model, step="module:generate", module_source=mod_src, input_data=messages)
            from litellm import acompletion
            response = await acompletion(model=model, messages=messages, max_tokens=max_tokens)
            content = response.choices[0].message.content or ""
            if self._inspector and cid:
                await self._inspector.end_call(cid, prompt, content, 0, 0)
            return content
        except Exception as e:
            logger.error(f"Module LLM call failed (model={model}): {e}")
            return ""

    def _pick_model(self, preference: str) -> str:
        if preference == "fastest":
            return self._fast_model
        elif preference == "smartest":
            return self._storyteller_model
        return self._reader_model
