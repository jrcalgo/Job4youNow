"""Model-catalog vocabulary — mirrors `cursor_sdk`'s own model shapes at the
boundary (same pattern `CursorRunResult.from_sdk_result` already uses for
run results) so nothing outside tools/model_catalog.py and
tools/cursor_sdk_tool.py ever imports `cursor_sdk` types directly.

Cursor's model API exposes configurable settings generically: each model
reports its own `parameters` (e.g. a reasoning-effort knob, if it has one)
and `variants` (curated preset combinations of those parameters). There is
no dedicated "context window" field — that is normally fixed per model, not
user-selectable — so nothing here invents one.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent.app.models.tasks import TaskKind


class ModelParamValueOption(BaseModel):
    """One selectable value for a `ModelParamDefinition`, e.g. value="high",
    display_name="High"."""

    value: str
    display_name: str

    @classmethod
    def from_sdk(cls, sdk_value: Any) -> "ModelParamValueOption":
        return cls(value=sdk_value.value, display_name=sdk_value.display_name)


class ModelParamDefinition(BaseModel):
    """One configurable setting a model reports, e.g. id="reasoning_effort"
    with a handful of `values` to choose from. Purely data-driven — never
    hardcode which parameters exist; render whatever a model's live
    response actually contains."""

    id: str
    display_name: str
    values: list[ModelParamValueOption] = Field(default_factory=list)

    @classmethod
    def from_sdk(cls, sdk_param: Any) -> "ModelParamDefinition":
        return cls(
            id=sdk_param.id,
            display_name=sdk_param.display_name,
            values=[ModelParamValueOption.from_sdk(v) for v in sdk_param.values],
        )


class ModelParamChoice(BaseModel):
    """One chosen parameter value — mirrors `cursor_sdk.ModelParameterValue`
    exactly (`id` referencing a `ModelParamDefinition.id`, `value` one of
    its `values`). This is the shape both a `ModelVariantInfo.params` entry
    and a persisted `ModelConfig.params` entry use."""

    id: str
    value: str

    @classmethod
    def from_sdk(cls, sdk_param_value: Any) -> "ModelParamChoice":
        return cls(id=sdk_param_value.id, value=sdk_param_value.value)


class ModelVariantInfo(BaseModel):
    """A curated preset combination of parameter choices for one model,
    e.g. "Thinking" = reasoning_effort=high. `is_default` marks the variant
    Cursor itself would pick if none is specified."""

    display_name: str
    description: str
    is_default: bool
    params: list[ModelParamChoice] = Field(default_factory=list)

    @classmethod
    def from_sdk(cls, sdk_variant: Any) -> "ModelVariantInfo":
        return cls(
            display_name=sdk_variant.display_name,
            description=sdk_variant.description,
            is_default=sdk_variant.is_default,
            params=[ModelParamChoice.from_sdk(p) for p in sdk_variant.params],
        )


class ModelCatalogEntry(BaseModel):
    """One model from a live `Cursor.models.list()` / `AsyncCursor.models.list()`
    response, converted to this app's own type at the boundary."""

    id: str
    display_name: str
    description: str
    parameters: list[ModelParamDefinition] = Field(default_factory=list)
    variants: list[ModelVariantInfo] = Field(default_factory=list)

    @classmethod
    def from_sdk_model(cls, sdk_model: Any) -> "ModelCatalogEntry":
        return cls(
            id=sdk_model.id,
            display_name=sdk_model.display_name,
            description=sdk_model.description,
            parameters=[ModelParamDefinition.from_sdk(p) for p in sdk_model.parameters],
            variants=[ModelVariantInfo.from_sdk(v) for v in sdk_model.variants],
        )


class ModelConfig(BaseModel):
    """Persisted, one row per `(chat_id, task_kind)` in `agent_model_config`
    — see db/model_config_repo.py. This is operator configuration, not
    LLM-derived domain data, so it does not go through db/db_gate.py."""

    chat_id: str
    task_kind: TaskKind
    model_id: str
    model_display_name: str
    params: list[ModelParamChoice] = Field(default_factory=list)

    def to_model_selection(self):
        """Builds the exact `cursor_sdk.ModelSelection` shape
        `AsyncAgent.create(model=...)` accepts directly. Imported lazily so
        this module (and everything that only needs the Pydantic shapes
        above) never has to import `cursor_sdk` at all."""
        from cursor_sdk import ModelParameterValue, ModelSelection

        return ModelSelection(id=self.model_id, params=[ModelParameterValue(id=p.id, value=p.value) for p in self.params])


class ModelWizardState(BaseModel):
    """Ephemeral, cache-only progress through the Telegram model-config
    wizard — never persisted to Aurora. Holds the last live-fetched model
    list so later callbacks can reference a model/parameter/value by a
    short INDEX rather than embedding a full id/value string, which is what
    keeps every `callback_data` well under Telegram's 64-byte limit.
    Losing this on a restart mid-wizard is fine — the user just restarts
    the flow with the `MODELS` command.
    """

    task_kind: TaskKind
    models: list[ModelCatalogEntry] = Field(default_factory=list)
    selected_model_index: int | None = None
    chosen_params: dict[str, str] = Field(default_factory=dict)
    pending_param_index: int | None = None

    @property
    def selected_model(self) -> ModelCatalogEntry | None:
        if self.selected_model_index is None:
            return None
        if 0 <= self.selected_model_index < len(self.models):
            return self.models[self.selected_model_index]
        return None
