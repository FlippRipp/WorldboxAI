"""LLM access bridge exposed to modules through the WorldBox SDK."""
import os
import logging

logger = logging.getLogger(__name__)


class LLMBridge:
    def __init__(self):
        self._service = None
        self._inspector = None
        self._current_module = ""
        self._storyteller_model = os.getenv("STORYTELLER_MODEL", "gemini/gemini-2.5-flash")
        self._reader_model = os.getenv("READER_MODEL", "gemini/gemini-2.5-flash")
        self._fast_model = os.getenv("MODULE_FAST_MODEL", self._reader_model)
        self._mode = os.getenv("LLM_MODE", "live").strip().lower()

    def _set_service(self, service):
        self._service = service
        self._storyteller_model = service.storyteller_model
        self._reader_model = service.reader_model
        self._fast_model = os.getenv("MODULE_FAST_MODEL", getattr(service, "module_fast_model", self._reader_model))
        self._mode = service.mode
        if hasattr(service, 'inspector') and service.inspector:
            self._inspector = service.inspector

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
                cid = self._inspector.start_call(call_type="module_fast", model="mock", step="module:generate", module_source=mod_src)
                await self._inspector.end_call(cid, prompt, result, 0, 0)
            return result

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
                cid = self._inspector.start_call(call_type="module_fast", model=model, step="module:generate", module_source=mod_src)
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
