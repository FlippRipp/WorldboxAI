"""LLM-backed step generation: prompt construction + JSON-retry completion.

Ported from the legacy ``_generate_live`` / ``_json_retry_completion``. Per-step
guidance is read from ``step.guidance`` so each step owns its own prompt hints.
"""

import json
import logging

logger = logging.getLogger(__name__)

#: Historical default framing; world templates may swap it (see
#: wbworldgen.worldgen.templates.DEFAULT_SYSTEM_FRAMING, kept byte-identical).
_DEFAULT_SYSTEM_FRAMING = "You are a world building AI for a tabletop roleplaying game."


async def json_retry_completion(
    llm_service,
    messages: list[dict],
    model: str,
    temperature: float,
    inspector_ctx: dict,
    step_label: str = "step",
    retry_attempts: int = 2,
) -> dict:
    """Call the LLM expecting a JSON object, retrying on parse failure."""
    last_content = ""
    for attempt in range(retry_attempts + 1):
        try:
            content = await llm_service.simple_completion(
                messages=messages,
                model=model,
                response_format={"type": "json_object"},
                temperature=temperature,
                inspector_ctx=inspector_ctx,
            )
            parsed = json.loads(content)
            return parsed
        except json.JSONDecodeError as e:
            logger.warning(
                "JSON parse failed for %s (attempt %d/%d): %s",
                step_label, attempt + 1, retry_attempts + 1, e,
            )
            if attempt < retry_attempts:
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your response was not valid JSON. Error: {e}.\n"
                        "Please respond with ONLY valid JSON. Do not include any "
                        "text outside the JSON object."
                    ),
                })
        except Exception as e:
            logger.error("World generation failed for %s: %s", step_label, e)
            raise
    logger.error(
        "JSON retry exhausted for %s after %d attempts. Last content: %.500s",
        step_label, retry_attempts + 1, last_content,
    )
    raise ValueError(
        f"Failed to generate valid JSON for {step_label} after {retry_attempts + 1} attempts"
    )


def _directive_block(coverage_directive: str) -> str:
    """The world_form directive for this step, rendered for prompt injection.
    Empty directive -> empty string, keeping prompts byte-identical for worlds
    without a world design (old worlds, mock worlds)."""
    if not coverage_directive:
        return ""
    return (
        f"For THIS world, this step should cover: {coverage_directive}\n"
        "(This directive comes from the world-design pass for this specific world; "
        "where it conflicts with the generic guidance below, the directive wins.)\n\n"
    )


class LLMStepGenerator:
    """Generates a step's output dict via the LLM."""

    def __init__(self, llm_service=None, settings=None, model=None, temperature=None, retry_attempts: int = 2):
        self._llm = llm_service
        self._settings = settings
        self._model = model
        self._temperature = temperature
        self._retry_attempts = retry_attempts

    @property
    def llm(self):
        return self._llm

    async def generate(self, step, context: dict, user_prompt: str, user_note: str = "",
                       system_framing: str = None, coverage_directive: str = "") -> dict:
        model = self._model or self._llm.reader_model
        temperature = self._temperature or 1.0

        narrative = ""
        if self._settings:
            ns = self._settings.get("world.narrative_style")
            if ns:
                narrative = " Narrative style: " + ns

        system = (
            f"{system_framing or _DEFAULT_SYSTEM_FRAMING}\n"
            f"Generate a structured {step.label} for a world based on the user's prompt.\n"
            f"Output only valid JSON matching the requested schema.{narrative}"
        )

        guidance = getattr(step, "guidance", "") or ""

        user_msg = f"""World seed prompt: {user_prompt}

Step: {step.label} — {step.description}

{_directive_block(coverage_directive)}{("Guidance: " + guidance) if guidance else ""}

{"Chain context from previous steps:" + json.dumps(context, indent=2) if context else "This is the first step. No prior context."}

{f"User note: {user_note}" if user_note else ""}

Generate the {step.label} as a JSON object matching this field schema (each entry describes the
expected type/label for that field — it is NOT example content, do not copy it verbatim):
{json.dumps(step.schema, indent=2)}

Your response must contain actual generated world content for every field — real names, descriptions,
and values appropriate to the world seed prompt above. Never output the schema's own keys
("type", "label", "item_schema", "description", "min", "max", "default") as if they were data."""

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        inspector_ctx = {"call_type": "world_build", "step": f"world_build:{step.id}"}

        return await json_retry_completion(
            self._llm,
            messages=messages,
            model=model,
            temperature=temperature,
            inspector_ctx=inspector_ctx,
            step_label=step.label,
            retry_attempts=self._retry_attempts,
        )

    async def generate_list_item(
        self, step, field_key: str, field_schema: dict, items: list,
        index: int, context: dict, user_prompt: str, user_note: str = "",
        system_framing: str = None, coverage_directive: str = "",
    ) -> str:
        """Regenerate a single entry of a list field, distinct from the others."""
        model = self._model or self._llm.reader_model
        # Slightly above the step temperature for more variety on a single item.
        temperature = min((self._temperature or 1.0) + 0.1, 1.0)

        field_label = field_schema.get("label", field_key)
        field_desc = field_schema.get("description", "")
        guidance = getattr(step, "guidance", "") or ""

        others = [it for i, it in enumerate(items) if i != index and it]
        current = items[index] if 0 <= index < len(items) else ""

        system = (
            f"{system_framing or _DEFAULT_SYSTEM_FRAMING}\n"
            f"You regenerate ONE entry of the '{field_label}' list for the world's {step.label}.\n"
            'Output only a JSON object of the form {"item": "<the single new entry>"}.'
        )

        user_msg = f"""World seed prompt: {user_prompt}

Step: {step.label} — {step.description}
Field: {field_label}{(" — " + field_desc) if field_desc else ""}

{_directive_block(coverage_directive)}{("Guidance: " + guidance) if guidance else ""}

{"Context from previous steps:" + json.dumps(context, indent=2) if context else ""}

{f"User note: {user_note}" if user_note else ""}

Existing entries (do NOT duplicate these):
{json.dumps(others, indent=2) if others else "(none yet)"}

{f'The entry being replaced was: "{current}". Produce a fresh, different one.' if current else ""}

Respond with a single new '{field_label}' entry as JSON: {{"item": "..."}}"""

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        inspector_ctx = {"call_type": "world_build", "step": f"world_build:{step.id}:{field_key}"}

        result = await json_retry_completion(
            self._llm,
            messages=messages,
            model=model,
            temperature=temperature,
            inspector_ctx=inspector_ctx,
            step_label=f"{step.label} · {field_label} item",
            retry_attempts=self._retry_attempts,
        )
        item = result.get("item", "")
        if isinstance(item, (list, dict)):
            item = json.dumps(item)
        return str(item).strip()

    async def generate_structured_item(
        self, step, field_key: str, field_schema: dict, items: list,
        index: int, context: dict, user_prompt: str, user_note: str = "",
        subfield: str = None, system_framing: str = None, coverage_directive: str = "",
    ):
        """Regenerate one structured (object) entry of a list field, or a single
        sub-field of that entry when ``subfield`` is given.

        Returns a dict (whole entry) when ``subfield`` is None, otherwise the new
        value for that sub-field only.
        """
        model = self._model or self._llm.reader_model
        # Slightly above the step temperature for more variety on a single item.
        temperature = min((self._temperature or 1.0) + 0.1, 1.0)

        field_label = field_schema.get("label", field_key)
        item_schema = field_schema.get("item_schema", {})
        guidance = getattr(step, "guidance", "") or ""

        others = [it for i, it in enumerate(items) if i != index and it]
        current = items[index] if 0 <= index < len(items) else {}

        if subfield:
            sub_schema = item_schema.get(subfield, {})
            sub_label = sub_schema.get("label", subfield) if isinstance(sub_schema, dict) else subfield
            system = (
                f"{system_framing or _DEFAULT_SYSTEM_FRAMING}\n"
                f"You regenerate ONLY the '{sub_label}' field of one entry in the "
                f"'{field_label}' list for the world's {step.label}.\n"
                'Output only a JSON object of the form {"item": <the single new value>}.'
            )
            task = f"""Regenerate ONLY the '{sub_label}' field. Keep the rest of the entry as-is.
The entry being edited (do not change its other fields):
{json.dumps(current, indent=2)}

This field follows the schema: {json.dumps(sub_schema, indent=2) if isinstance(sub_schema, dict) else sub_schema}

Respond with the new value as JSON: {{"item": <value>}}"""
        else:
            system = (
                f"{system_framing or _DEFAULT_SYSTEM_FRAMING}\n"
                f"You regenerate ONE entry of the '{field_label}' list for the world's {step.label}.\n"
                'Output only a JSON object of the form {"item": {<the single new entry>}}.'
            )
            task = f"""Regenerate ONE entry as a fresh, different alternative to the one being replaced.
The entry being replaced was:
{json.dumps(current, indent=2) if current else "(none)"}

Produce a new entry as a JSON object matching this item schema (each value describes the expected
type/label — it is NOT example content, do not copy it verbatim):
{json.dumps(item_schema, indent=2)}

Respond with the new entry as JSON: {{"item": {{ ... }}}}"""

        user_msg = f"""World seed prompt: {user_prompt}

Step: {step.label} — {step.description}
Field: {field_label}

{_directive_block(coverage_directive)}{("Guidance: " + guidance) if guidance else ""}

{"Context from previous steps:" + json.dumps(context, indent=2) if context else ""}

{f"User note: {user_note}" if user_note else ""}

Existing entries (do NOT duplicate these):
{json.dumps(others, indent=2) if others else "(none yet)"}

{task}"""

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        sub_tag = f":{subfield}" if subfield else ""
        inspector_ctx = {"call_type": "world_build", "step": f"world_build:{step.id}:{field_key}{sub_tag}"}

        result = await json_retry_completion(
            self._llm,
            messages=messages,
            model=model,
            temperature=temperature,
            inspector_ctx=inspector_ctx,
            step_label=f"{step.label} · {field_label} entry",
            retry_attempts=self._retry_attempts,
        )
        return result.get("item", "" if subfield else {})
