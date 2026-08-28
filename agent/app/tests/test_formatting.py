"""Snapshot-style assertions on presenters/chunking/escaping — the output
policy's actual enforcement point. If a presenter starts leaking unescaped
HTML or an unchunked 5000-character message, these are what catch it.
"""
from __future__ import annotations

import pytest

from agent.app.formatting.chunking import TELEGRAM_MAX_MESSAGE_LENGTH, chunk_text, to_outbound_messages
from agent.app.formatting.llm_output import normalize_llm_text
from agent.app.formatting.presenters import Presenters
from agent.app.formatting.telegram_markdown import escape_html
from agent.app.models.artifacts import ArtifactBucket, ArtifactLocation, ContentVisibility, PrivateArtifactMetadata
from agent.app.models.db_writes import DbWriteReceipt
from agent.app.models.domain import JobListingRow, RoleBacklog
from agent.app.models.model_catalog import ModelCatalogEntry, ModelConfig, ModelParamChoice, ModelParamDefinition, ModelParamValueOption, ModelVariantInfo
from agent.app.models.responses import UserFacingResponse
from agent.app.models.tasks import ScanRolePayload, TaskKind, TaskSource, TaskStatus, TaskSubmission
from agent.app.models.telegram import DeliveryKind, TelegramInlineButton, TelegramOutboundMessage

presenters = Presenters()
_PUBLIC = ContentVisibility.PUBLIC_JOB_SEARCH
_PRIVATE = ContentVisibility.PRIVATE_USER


def _artifact_location() -> ArtifactLocation:
    return ArtifactLocation(bucket=ArtifactBucket.PRIVATE_USER_ARTIFACTS, key="k", checksum_sha256="abc", byte_size=3)


def _private_artifact() -> PrivateArtifactMetadata:
    return PrivateArtifactMetadata(chat_id="42", kind="augmented_resume", location=_artifact_location())


def test_escape_html_covers_all_five_special_characters() -> None:
    assert escape_html('<b>"Tom" & Jerry</b>') == "&lt;b&gt;&quot;Tom&quot; &amp; Jerry&lt;/b&gt;"


def test_chunk_text_leaves_short_text_untouched() -> None:
    assert chunk_text("short message") == ["short message"]


def test_chunk_text_splits_long_text_under_the_limit() -> None:
    long_text = "word " * 2000  # well over 4096 chars
    chunks = chunk_text(long_text)
    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_MAX_MESSAGE_LENGTH for chunk in chunks)


def test_chunk_text_labels_multi_part_messages() -> None:
    long_text = "word " * 2000
    chunks = chunk_text(long_text, label_prefix="Digest")
    assert chunks[0].startswith(f"Digest 1/{len(chunks)}")
    assert chunks[-1].startswith(f"Digest {len(chunks)}/{len(chunks)}")


def test_to_outbound_messages_attaches_buttons_only_to_last_chunk() -> None:
    response = UserFacingResponse(
        visibility=_PUBLIC,
        body="word " * 2000,
        buttons=[[TelegramInlineButton(text="Next", callback_data="next")]],
        label_prefix="Digest",
    )
    messages = to_outbound_messages(response, "42")
    assert len(messages) > 1
    assert all(not message.buttons for message in messages[:-1])
    assert messages[-1].buttons
    assert all(message.delivery_kind == DeliveryKind.PUBLIC_TEXT for message in messages)


def test_to_outbound_messages_delivers_a_materialized_private_response_as_one_artifact_message() -> None:
    response = UserFacingResponse(visibility=_PRIVATE, title="Resume updated", private_artifact=_private_artifact())
    messages = to_outbound_messages(response, "42")
    assert len(messages) == 1
    assert messages[0].delivery_kind == DeliveryKind.PRIVATE_ARTIFACT
    assert messages[0].artifact == _private_artifact().location
    assert messages[0].text is None


def test_to_outbound_messages_rejects_an_unmaterialized_private_response() -> None:
    response = UserFacingResponse(visibility=_PRIVATE, body="still has inline body text")
    with pytest.raises(ValueError, match="unmaterialized"):
        to_outbound_messages(response, "42")


def test_user_facing_response_requires_body_or_private_artifact() -> None:
    with pytest.raises(ValueError):
        UserFacingResponse(visibility=_PUBLIC)


def test_telegram_outbound_message_rejects_public_text_kind_without_text() -> None:
    with pytest.raises(ValueError):
        TelegramOutboundMessage(chat_id="42", delivery_kind=DeliveryKind.PUBLIC_TEXT)


def test_telegram_outbound_message_rejects_artifact_kind_without_artifact() -> None:
    with pytest.raises(ValueError):
        TelegramOutboundMessage(chat_id="42", delivery_kind=DeliveryKind.PRIVATE_ARTIFACT)


def test_backlog_presenter_empty_state() -> None:
    response = presenters.backlog([])
    assert "No scanned jobs" in response.body


def test_backlog_presenter_lists_roles_and_buttons() -> None:
    roles = [RoleBacklog(role_id="backend", pending_count=3), RoleBacklog(role_id="frontend", pending_count=1)]
    response = presenters.backlog(roles)
    assert "backend" in response.body
    assert response.buttons[1][0].callback_data == "role:backend"


def test_job_list_presenter_handles_empty_list() -> None:
    response = presenters.job_list("backend", [])
    assert "Nothing pending" in response.body


def test_job_list_presenter_escapes_company_name() -> None:
    listing = JobListingRow(id="1", role_id="backend", company_name="<Acme>", title="SWE", summary=["Remote"], status="pending")
    response = presenters.job_list("backend", [listing])
    assert "<Acme>" not in response.body
    assert "&lt;Acme&gt;" in response.body


def test_task_status_presenter_reports_failure_reason() -> None:
    task = TaskSubmission(
        chat_id="42", source=TaskSource.USER, payload=ScanRolePayload(role_id="backend", query="python"), idempotency_key="k"
    ).to_task()
    failed_task = task.model_copy(update={"status": TaskStatus.FAILED, "error": "timeout"})
    response = presenters.task_status(failed_task)
    assert "timeout" in response.body


def test_scan_summary_presenter_distinguishes_zero_from_nonzero() -> None:
    empty_receipt = DbWriteReceipt.noop("k")
    nonzero_receipt = DbWriteReceipt(idempotency_key="k", applied=True, counts={"job_listings": 2, "contacts": 1})
    assert "no new listings" in presenters.scan_summary("backend", empty_receipt).body
    assert "2 new listing" in presenters.scan_summary("backend", nonzero_receipt).body


def test_backlog_presenter_is_public() -> None:
    assert presenters.backlog([]).visibility == _PUBLIC


def test_research_result_presenter_defaults_to_private() -> None:
    response = presenters.research_result("Some free-text answer.")
    assert response.visibility == _PRIVATE
    assert response.body is not None  # still a staging value at this point, not yet materialized


def test_resume_result_presenter_is_always_private_with_the_given_artifact() -> None:
    artifact = _private_artifact()
    response = presenters.resume_result(artifact)
    assert response.visibility == _PRIVATE
    assert response.private_artifact == artifact
    assert response.body is None


def test_normalize_llm_text_drops_tool_chatter_lines() -> None:
    raw = "I'll check the database now.\nHere is the actual answer.\n\n\n\nDone."
    normalized = normalize_llm_text(raw)
    assert "I'll check" not in normalized
    assert "Here is the actual answer." in normalized
    assert "\n\n\n" not in normalized


# -- Model-configuration wizard presenters --


def _model_with_variant_and_param() -> ModelCatalogEntry:
    return ModelCatalogEntry(
        id="grok-4.5",
        display_name="Grok 4.5",
        description="Fast, general-purpose model.",
        parameters=[
            ModelParamDefinition(
                id="reasoning_effort",
                display_name="Reasoning effort",
                values=[
                    ModelParamValueOption(value="low", display_name="Low"),
                    ModelParamValueOption(value="high", display_name="High"),
                ],
            )
        ],
        variants=[
            ModelVariantInfo(display_name="Thinking", description="Higher effort.", is_default=True, params=[]),
            ModelVariantInfo(display_name="Fast", description="Lower effort.", is_default=False, params=[]),
        ],
    )


def test_model_task_menu_is_public_and_lists_every_task_kind() -> None:
    response = presenters.model_task_menu({}, "grok-4.5")
    assert response.visibility == _PUBLIC
    assert len(response.buttons) == 4
    task_buttons = {row[0].callback_data for row in response.buttons[1:]}
    assert task_buttons == {"modeltask:scan_role", "modeltask:augment_resume", "modeltask:research_company"}


def test_model_task_menu_shows_the_configured_model_instead_of_the_default() -> None:
    config = ModelConfig(chat_id="42", task_kind=TaskKind.SCAN_ROLE, model_id="grok-4.5", model_display_name="Grok 4.5 (custom)")
    response = presenters.model_task_menu({TaskKind.SCAN_ROLE: config}, "grok-4.5")

    scan_role_line = next(line for line in response.body.splitlines() if "Scan roles" in line)
    assert "Grok 4.5 (custom)" in scan_role_line
    assert "(default)" not in scan_role_line
    # The other two task kinds have no saved config, so they still show the default.
    assert response.body.count("(default)") == 2


def test_model_list_marks_the_current_model_and_offers_refresh_and_back() -> None:
    models = [_model_with_variant_and_param(), ModelCatalogEntry(id="other", display_name="Other", description="", parameters=[], variants=[])]
    response = presenters.model_list(TaskKind.SCAN_ROLE, models, current_model_id="other")

    assert "\u2713" in response.buttons[2][0].text  # the current model is marked
    assert "\u2713" not in response.buttons[1][0].text
    callback_data_values = [row[0].callback_data for row in response.buttons[1:]]
    assert "modelrefresh" in callback_data_values
    assert "modelback" in callback_data_values


def test_model_list_handles_an_empty_catalog_response() -> None:
    response = presenters.model_list(TaskKind.SCAN_ROLE, [], current_model_id=None)
    assert "No models" in response.body
    assert response.buttons[1][0].callback_data == "modelrefresh"


def test_model_detail_marks_the_default_variant_and_offers_customize() -> None:
    response = presenters.model_detail(TaskKind.SCAN_ROLE, _model_with_variant_and_param())

    labels = [row[0].text for row in response.buttons]
    assert any("Thinking" in label and "default" in label for label in labels)
    assert any("Fast" in label and "default" not in label for label in labels)
    callback_data_values = [row[0].callback_data for row in response.buttons]
    assert "modelcustom" in callback_data_values
    assert "modeldefault" in callback_data_values
    assert "modelback" in callback_data_values


def test_model_detail_omits_customize_button_for_a_model_with_no_parameters() -> None:
    model = ModelCatalogEntry(id="plain", display_name="Plain", description="", parameters=[], variants=[])
    response = presenters.model_detail(TaskKind.SCAN_ROLE, model)

    callback_data_values = [row[0].callback_data for row in response.buttons]
    assert "modelcustom" not in callback_data_values
    assert "modeldefault" in callback_data_values


def test_model_param_step_renders_one_button_per_value_and_step_progress() -> None:
    model = _model_with_variant_and_param()
    response = presenters.model_param_step(TaskKind.SCAN_ROLE, model, model.parameters[0], step_index=0, total_steps=2)

    assert "step 1 of 2" in response.title.lower()
    labels = [row[0].text for row in response.buttons]
    assert "Low" in labels
    assert "High" in labels


def test_model_config_saved_summarizes_chosen_params() -> None:
    config = ModelConfig(
        chat_id="42",
        task_kind=TaskKind.AUGMENT_RESUME,
        model_id="grok-4.5",
        model_display_name="Grok 4.5",
        params=[ModelParamChoice(id="reasoning_effort", value="high")],
    )
    response = presenters.model_config_saved(TaskKind.AUGMENT_RESUME, config)

    assert "Grok 4.5" in response.body
    assert "reasoning_effort=high" in response.body
    assert response.visibility == _PUBLIC


def test_model_config_saved_without_params_mentions_default_settings() -> None:
    config = ModelConfig(chat_id="42", task_kind=TaskKind.SCAN_ROLE, model_id="grok-4.5", model_display_name="Grok 4.5")
    response = presenters.model_config_saved(TaskKind.SCAN_ROLE, config)
    assert "default settings" in response.body
