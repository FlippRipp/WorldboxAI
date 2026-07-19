"""Core data types for the modular world generation pipeline.

`PipelineStep` is kept backwards-compatible with the legacy
`backend.engine.world_builder.PipelineStep` dataclass (same field names,
including the legacy ``id`` and optional ``generate`` fields) so existing
callers and tests continue to work unchanged.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class PipelineStep:
    """Immutable definition of a pipeline step.

    Backwards-compatible with the legacy dataclass: uses ``id`` (not
    ``step_id``) and keeps the optional ``generate`` callable field.
    """
    id: str
    label: str
    description: str
    after: Optional[str]
    generate: Optional[Callable] = None
    schema: dict[str, Any] = field(default_factory=dict)
    #: Optional per-step LLM guidance text (used by the LLM generator).
    guidance: str = ""

    def to_frontend(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "after": self.after,
            "schema": self.schema,
        }


@dataclass
class StepResult:
    """Runtime result of one step within a generation session."""
    data: dict[str, Any]
    approved: bool = False
    note: str = ""


@dataclass
class WorldState:
    """All data produced during a world generation session.

    The canonical runtime representation throughout the app remains a plain
    ``dict`` (for API/JSON compatibility); this dataclass is a typed helper
    used where structured access is convenient.
    """
    seed_prompt: str
    steps: dict[str, StepResult] = field(default_factory=dict)
    current_step: Optional[str] = None
    complete: bool = False

    def is_approved(self, step_id: str) -> bool:
        return self.steps.get(step_id, StepResult({})).approved

    def next_unapproved(self, ordered: list[str]) -> Optional[str]:
        for sid in ordered:
            if sid in self.steps and not self.steps[sid].approved:
                return sid
        return None

    def invalidate_downstream(self, from_step: str, ordered: list[str]) -> None:
        try:
            idx = ordered.index(from_step)
        except ValueError:
            return
        for sid in ordered[idx + 1:]:
            if sid in self.steps:
                self.steps[sid].approved = False
        self.complete = False

    def to_json_serializable(self) -> dict:
        steps_dict = {
            sid: {"data": sr.data, "approved": sr.approved, "note": sr.note}
            for sid, sr in self.steps.items()
        }
        return {
            "seed_prompt": self.seed_prompt,
            "steps": steps_dict,
            "complete": self.complete,
            "current_step": self.current_step,
        }

    @classmethod
    def from_json_serializable(cls, data: dict) -> "WorldState":
        steps = {
            sid: StepResult(
                data=sd.get("data", {}),
                approved=sd.get("approved", False),
                note=sd.get("note", ""),
            )
            for sid, sd in data.get("steps", {}).items()
        }
        return cls(
            seed_prompt=data.get("seed_prompt", ""),
            steps=steps,
            complete=data.get("complete", False),
            current_step=data.get("current_step"),
        )


@dataclass
class StepContext:
    """Everything a step needs to generate its output.

    Passed to a step's optional ``generate(ctx)`` override so custom steps can
    produce output without reaching back into the orchestrator.
    """
    step: Any
    world_state: dict
    user_prompt: str
    user_note: str = ""
    config: Optional[dict] = None
    services: Any = None
    #: Forces the step's mock/offline path even when a live LLM is wired
    #: (seeded worlds must be deterministic and never spend tokens).
    force_mock: bool = False
