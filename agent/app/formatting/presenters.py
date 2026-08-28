"""One method per kind of Telegram output. Every method here is a pure
function of typed input to a UserFacingResponse — no I/O, no LLM calls, so
these are trivial to snapshot-test (see tests/test_presenters.py).

Every method sets `visibility` explicitly — never left for a caller to
infer. Deterministic reads (backlog, job_list, task_status, scan_summary)
are PUBLIC_JOB_SEARCH: they only ever describe job-search domain data or
operational task state, never user-submitted content. `research_result`
defaults to PRIVATE_USER because a free-text request may reference the
user's own situation; `resume_result` is unconditionally PRIVATE_USER.
"""
from __future__ import annotations

from datetime import datetime

from agent.app.formatting.llm_output import normalize_llm_text
from agent.app.formatting.telegram_markdown import bold, bullet_list, escape_html
from agent.app.models.artifacts import ContentVisibility, PrivateArtifactMetadata
from agent.app.models.db_writes import DbWriteReceipt
from agent.app.models.domain import JobListingRow, RoleBacklog
from agent.app.models.model_catalog import ModelCatalogEntry, ModelConfig, ModelParamDefinition
from agent.app.models.responses import UserFacingResponse
from agent.app.models.schedules import ScanSchedule
from agent.app.models.tasks import AgentTask, TaskKind, TaskStatus
from agent.app.models.telegram import TelegramInlineButton

_MAX_BACKLOG_BUTTONS = 10
_MAX_LISTINGS_SHOWN = 15
_PUBLIC = ContentVisibility.PUBLIC_JOB_SEARCH
_PRIVATE = ContentVisibility.PRIVATE_USER

def _format_posted_at(posted_at: datetime | None) -> str | None:
    return f"posted {posted_at.date().isoformat()}" if posted_at else None


_TASK_KIND_LABELS: dict[TaskKind, str] = {
    TaskKind.SCAN_ROLE: "Scan roles",
    TaskKind.AUGMENT_RESUME: "Resume augmentation",
    TaskKind.RESEARCH_COMPANY: "Research / company queries",
}


def _with_nav_rows(
    buttons: list[list[TelegramInlineButton]],
    *,
    show_main: bool = True,
) -> list[list[TelegramInlineButton]]:
    """Prepend a Main menu row to every inline keyboard."""
    if not show_main:
        return buttons
    nav = [[TelegramInlineButton(text="Main menu", callback_data="main")]]
    return nav + buttons


class Presenters:
    def main_menu(self) -> UserFacingResponse:
        body = (
            "Welcome to Job4youNow. Tap a button below, or type a command.\n\n"
            "Backlog — current job backlog by role\n"
            "Settings — models, scan schedules, backlog filters\n"
            "Models — choose which Cursor SDK model each task uses\n"
            "Queues — switch between ingested job-review queues\n"
            "Help — full command list\n\n"
            "Typed only:\n"
            "SCAN &lt;role&gt; &lt;query&gt; — start a scan\n"
            "STATUS &lt;task id&gt; — check a task's progress\n"
            "SCHEDULE &lt;role&gt; &lt;query&gt; every &lt;hours&gt;h — recurring scan\n"
            "RESUME &lt;role&gt; :: &lt;job description&gt; — tailor your resume\n"
            "Or ask the agent app a question in plain text."
        )
        return UserFacingResponse(
            visibility=_PUBLIC,
            title="Job4youNow",
            body=body,
            buttons=_with_nav_rows(
                [
                    [
                        TelegramInlineButton(text="Backlog", callback_data="backlog"),
                        TelegramInlineButton(text="Settings", callback_data="settingsmenu"),
                    ],
                    [
                        TelegramInlineButton(text="Models", callback_data="modelmenu"),
                        TelegramInlineButton(text="Queues", callback_data="tg:menu:queues"),
                    ],
                    [TelegramInlineButton(text="Help", callback_data="tg:menu:help")],
                ],
                show_main=False,
            ),
        )

    def settings_menu(self) -> UserFacingResponse:
        return UserFacingResponse(
            visibility=_PUBLIC,
            title="Settings",
            body=(
                "Models — Cursor SDK model per task type.\n"
                "Schedules — background scan frequency per role.\n"
                "Backlog filters — default listing filters."
            ),
            buttons=_with_nav_rows(
                [
                    [TelegramInlineButton(text="Models", callback_data="settings:models")],
                    [TelegramInlineButton(text="Schedules", callback_data="settings:schedules")],
                    [TelegramInlineButton(text="Backlog filters", callback_data="settings:backlog")],
                ],
            ),
        )

    def backlog(self, roles: list[RoleBacklog]) -> UserFacingResponse:
        if not roles:
            return UserFacingResponse(
                visibility=_PUBLIC,
                title="Backlog",
                body="No scanned jobs are waiting for review yet.",
                buttons=_with_nav_rows([
                    [TelegramInlineButton(text="Settings", callback_data="settingsmenu")],
                ]),
            )

        lines = [f"• {bold(role.role_id)} — {role.pending_count} pending" for role in roles]
        return UserFacingResponse(
            visibility=_PUBLIC,
            title="Current job backlog",
            body="\n".join(lines),
            buttons=_with_nav_rows(
                [
                    [TelegramInlineButton(text=f"{role.role_id} ({role.pending_count})", callback_data=f"role:{role.role_id}")]
                    for role in roles[:_MAX_BACKLOG_BUTTONS]
                ],
            ),
        )

    def job_list(self, role_id: str, listings: list[JobListingRow]) -> UserFacingResponse:
        if not listings:
            return UserFacingResponse(
                visibility=_PUBLIC,
                title=f"{role_id} backlog",
                body="Nothing pending for this role right now.",
                buttons=_with_nav_rows([
                    [TelegramInlineButton(text="Back to backlog", callback_data="backlog")],
                ]),
            )

        sections = []
        for listing in listings[:_MAX_LISTINGS_SHOWN]:
            header = f"{bold(listing.company_name)} — {escape_html(listing.title)}"
            meta_parts = [p for p in (listing.location, _format_posted_at(listing.posted_at)) if p]
            meta = escape_html(" · ".join(meta_parts)) if meta_parts else ""
            summary = bullet_list(listing.summary) if listing.summary else ""
            link = f'\n<a href="{escape_html(listing.url)}">Listing</a>' if listing.url else ""
            sections.append("\n".join(part for part in (header, meta, summary) if part) + link)

        overflow = len(listings) - _MAX_LISTINGS_SHOWN
        body = "\n\n".join(sections)
        if overflow > 0:
            body += f"\n\n…and {overflow} more."

        return UserFacingResponse(
            visibility=_PUBLIC, title=f"{role_id} — {len(listings)} pending", body=body, label_prefix=f"{role_id} listings"
        )

    def task_status(self, task: AgentTask) -> UserFacingResponse:
        status_lines = {
            TaskStatus.PENDING: "queued, waiting for a worker",
            TaskStatus.RUNNING: "in progress",
            TaskStatus.SUCCEEDED: "done",
            TaskStatus.FAILED: f"failed — {task.error or 'no error detail recorded'}",
            TaskStatus.CANCELLED: "cancelled",
        }
        return UserFacingResponse(
            visibility=_PUBLIC,
            title=f"Task {task.id}",
            body=f"Kind: {escape_html(task.kind.value)}\nStatus: {escape_html(status_lines[task.status])}",
            buttons=_with_nav_rows([]),
        )

    def scan_summary(self, role_id: str, receipt: DbWriteReceipt) -> UserFacingResponse:
        new_listings = receipt.counts.get("job_listings", 0)
        new_contacts = receipt.counts.get("contacts", 0)
        if new_listings == 0:
            body = f"Scan finished for {bold(role_id)} — no new listings found."
        else:
            body = f"Scan finished for {bold(role_id)} — {new_listings} new listing(s), {new_contacts} contact(s)."
        return UserFacingResponse(
            visibility=_PUBLIC,
            body=body,
            buttons=_with_nav_rows([[TelegramInlineButton(text="View backlog", callback_data=f"role:{role_id}")]]),
        )

    def research_result(self, raw_text: str) -> UserFacingResponse:
        """PRIVATE_USER by default — a free-text request may reference the
        user's own resume/profile/situation, and there is no reliable way
        here to prove it doesn't. formatting/delivery.py materializes this
        before it is ever persisted or sent.

        Deliberately left UNESCAPED (unlike every public presenter's body)
        — this becomes the private artifact's stored bytes verbatim (see
        formatting/delivery.py's materialize_private_response), and that
        storage should stay plain, legible text/markdown rather than
        HTML-entity-escaped. telegram/src/agent/outbox.mjs's
        deliverPrivateArtifactAsText is the one place this ever needs to
        become Telegram-HTML-safe, and it escapes there, at delivery time —
        the only point where "safe for parse_mode=HTML" actually applies."""
        return UserFacingResponse(visibility=_PRIVATE, body=normalize_llm_text(raw_text), label_prefix="Research")

    def resume_result(self, artifact: PrivateArtifactMetadata, *, skill_gaps: list[str] | None = None) -> UserFacingResponse:
        """Takes the ALREADY-CONSTRUCTED PrivateArtifactMetadata (built once
        by graph/nodes.py's validate_output, alongside the matching
        PrivateArtifactWrite it hands to the DB gate) rather than a raw
        ResumeResult — one object, used both as the DB write payload and
        this response's pointer, never rebuilt a second time here.

        `skill_gaps` surfaces career-ops's own skill-gap classifier's `gap`
        bucket (see tools/resume_tool.py) directly in the artifact's
        caption — the ONLY text delivered alongside a PRIVATE_ARTIFACT
        message (see formatting/chunking.py), since `body` is never sent
        for that delivery kind."""
        title = "Resume tailored"
        if skill_gaps:
            gaps_text = escape_html(", ".join(skill_gaps))
            title = f"{title}\n\nYour CV doesn't show experience with: {gaps_text}"
        return UserFacingResponse(visibility=_PRIVATE, title=title, private_artifact=artifact)

    def schedule_confirmation(self, schedule: ScanSchedule) -> UserFacingResponse:
        hours = schedule.interval_seconds / 3600
        return UserFacingResponse(
            visibility=_PUBLIC,
            title="Schedule saved",
            body=f"Will scan {bold(schedule.role_id)} every {hours:g}h with query: {escape_html(schedule.query)}",
            buttons=_with_nav_rows([[TelegramInlineButton(text="View schedules", callback_data="sched:list")]]),
        )

    def schedule_list(self, schedules: list[ScanSchedule]) -> UserFacingResponse:
        if not schedules:
            body = "No recurring scans yet."
        else:
            lines = []
            for sched in schedules:
                hours = sched.interval_seconds / 3600
                flag = "on" if sched.enabled else "off"
                lines.append(
                    f"• {bold(sched.role_id)} every {hours:g}h ({flag}) — {escape_html(sched.query)}"
                )
            body = "\n".join(lines)
        buttons = [[TelegramInlineButton(text="Add schedule", callback_data="sched:add")]]
        for sched in schedules[:5]:
            buttons.append([
                TelegramInlineButton(text=f"Interval {sched.role_id}", callback_data=f"sched:edit:{sched.id}"),
                TelegramInlineButton(text=f"Toggle {sched.role_id}", callback_data=f"sched:toggle:{sched.id}"),
            ])
            buttons.append([
                TelegramInlineButton(text="Delete", callback_data=f"sched:del:{sched.id}"),
            ])
        return UserFacingResponse(visibility=_PUBLIC, title="Scan schedules", body=body, buttons=_with_nav_rows(buttons))

    def schedule_add_role_picker(self, role_ids: list[str]) -> UserFacingResponse:
        buttons = [
            [TelegramInlineButton(text=role_id, callback_data=f"sched:addrole:{role_id}")]
            for role_id in role_ids[:10]
        ]
        return UserFacingResponse(
            visibility=_PUBLIC,
            title="New schedule — pick role",
            body="Choose which role to scan on a schedule.",
            buttons=_with_nav_rows(buttons),
        )

    def schedule_edit_interval_picker(self, schedule: ScanSchedule) -> UserFacingResponse:
        hours = schedule.interval_seconds / 3600
        buttons = [
            [TelegramInlineButton(text=label, callback_data=f"sched:setint:{schedule.id}:{h}")]
            for label, h in [("6h", 6), ("12h", 12), ("24h", 24), ("168h", 168)]
        ]
        return UserFacingResponse(
            visibility=_PUBLIC,
            title=f"Change interval — {schedule.role_id}",
            body=f"Current: every {hours:g}h. New interval applies immediately.",
            buttons=_with_nav_rows(buttons),
        )

    def schedule_add_interval_picker(self, role_id: str) -> UserFacingResponse:
        buttons = [
            [TelegramInlineButton(text=label, callback_data=f"sched:interval:{role_id}:{hours}")]
            for label, hours in [("6h", 6), ("12h", 12), ("24h", 24), ("168h", 168)]
        ]
        return UserFacingResponse(
            visibility=_PUBLIC,
            title=f"New schedule — {role_id}",
            body="How often should this scan run?",
            buttons=_with_nav_rows(buttons),
        )

    def schedule_created(self, schedule: ScanSchedule) -> UserFacingResponse:
        return self.schedule_confirmation(schedule)

    def schedule_confirm_delete(self, schedule_id: str) -> UserFacingResponse:
        return UserFacingResponse(
            visibility=_PUBLIC,
            title="Delete schedule?",
            body="This cannot be undone.",
            buttons=_with_nav_rows([
                [TelegramInlineButton(text="Confirm delete", callback_data=f"sched:delconfirm:{schedule_id}")],
                [TelegramInlineButton(text="Cancel", callback_data="sched:list")],
            ]),
        )

    def backlog_card(
        self,
        listing: JobListingRow,
        *,
        offset: int,
        total: int,
        enqueue_button: TelegramInlineButton,
    ) -> UserFacingResponse:
        meta_parts = []
        if listing.work_mode:
            meta_parts.append(listing.work_mode)
        if listing.location:
            meta_parts.append(listing.location)
        if listing.posted_at:
            meta_parts.append(f"posted {listing.posted_at.date().isoformat()}")
        if listing.retrieved_at:
            meta_parts.append(f"retrieved {listing.retrieved_at.date().isoformat()}")
        if listing.applicant_count is not None:
            meta_parts.append(f"applicants: {listing.applicant_count}")
        else:
            meta_parts.append("applicants: —")
        meta = escape_html(" · ".join(meta_parts))
        header = f"{bold(listing.company_name)} — {escape_html(listing.title)}"
        summary = bullet_list(listing.summary) if listing.summary else ""
        link = f'\n<a href="{escape_html(listing.url)}">Listing</a>' if listing.url else ""
        body = "\n".join(part for part in (header, meta, summary) if part) + link
        pos = offset + 1 if total else 0
        nav_row = []
        if offset > 0:
            nav_row.append(TelegramInlineButton(text="◀ Prev", callback_data="bl:prev"))
        if offset + 1 < total:
            nav_row.append(TelegramInlineButton(text="Next ▶", callback_data="bl:next"))
        buttons = []
        if nav_row:
            buttons.append(nav_row)
        buttons.append([enqueue_button, TelegramInlineButton(text="Skip", callback_data="bl:mark:skipped")])
        buttons.append([TelegramInlineButton(text="Filter settings", callback_data="bl:settings")])
        return UserFacingResponse(
            visibility=_PUBLIC,
            title=f"Backlog {pos} / {total}",
            body=body,
            buttons=_with_nav_rows(buttons),
        )

    def backlog_filter_menu(
        self,
        filters,
        options: dict[str, list[str]],
        *,
        sort_key: str = "retrieved_at",
        sort_dir: str = "desc",
    ) -> UserFacingResponse:
        lines = [
            f"Sort: {escape_html(sort_key)} ({sort_dir})",
            f"Roles selected: {len(filters.role_ids)}",
            f"Companies selected: {len(filters.company_names)}",
            f"Work modes selected: {len(filters.work_modes)}",
            "Tap to toggle filters, then Save.",
        ]
        buttons = []
        for role_id in options.get("role_ids", [])[:8]:
            mark = "✓ " if role_id in filters.role_ids else ""
            buttons.append([TelegramInlineButton(text=f"{mark}{role_id}", callback_data=f"bl:fil:toggle:role:{role_id}")])
        for company in options.get("company_names", [])[:6]:
            mark = "✓ " if company in filters.company_names else ""
            short = company[:40]
            buttons.append([TelegramInlineButton(text=f"{mark}{short}", callback_data=f"bl:fil:toggle:company:{company}")])
        for mode in options.get("work_modes", []):
            mark = "✓ " if mode in filters.work_modes else ""
            buttons.append([TelegramInlineButton(text=f"{mark}{mode}", callback_data=f"bl:fil:toggle:work:{mode}")])
        buttons.append([
            TelegramInlineButton(text="Save filters", callback_data="bl:fil:save"),
            TelegramInlineButton(text="Clear all", callback_data="bl:fil:clear"),
        ])
        return UserFacingResponse(
            visibility=_PUBLIC,
            title="Backlog filters",
            body="\n".join(lines),
            buttons=_with_nav_rows(buttons),
        )

    def backlog_filters_saved(self) -> UserFacingResponse:
        return UserFacingResponse(
            visibility=_PUBLIC,
            title="Filters saved",
            body="Your backlog filter preferences were saved.",
            buttons=_with_nav_rows([
                [TelegramInlineButton(text="Open backlog", callback_data="backlog")],
                [TelegramInlineButton(text="Edit filters", callback_data="settings:backlog")],
            ]),
        )

    def error(self, message: str, *, next_action: str | None = None) -> UserFacingResponse:
        """Public: every call site passes an operational message (missing
        role, task not found, ...), never raw user content."""
        body = escape_html(message)
        if next_action:
            body += f"\n\n{escape_html(next_action)}"
        return UserFacingResponse(visibility=_PUBLIC, title="Something went wrong", body=body, buttons=_with_nav_rows([]))

    def help(self) -> UserFacingResponse:
        return UserFacingResponse(
            visibility=_PUBLIC,
            title="Commands",
            body=(
                "BACKLOG — current job backlog by role\n"
                "SCAN <role> <query> — start a scan\n"
                "STATUS <task id> — check a task's progress\n"
                "SCHEDULE <role> <query> every <hours>h — recurring scan\n"
                "MODELS — choose which Cursor SDK model each task uses\n"
                "SETTINGS — configuration menu\n"
                "Anything else is treated as a free-text request."
            ),
            buttons=_with_nav_rows([[TelegramInlineButton(text="Open main menu", callback_data="main")]]),
        )

    # -- Model configuration wizard — see routing/model_config.py, which
    # -- calls these in sequence as the user clicks through Telegram
    # -- buttons. All PUBLIC_JOB_SEARCH: operator configuration, never
    # -- user-private content.

    def model_task_menu(self, configs: dict[TaskKind, ModelConfig], default_model_id: str) -> UserFacingResponse:
        lines = []
        buttons = []
        for task_kind, label in _TASK_KIND_LABELS.items():
            config = configs.get(task_kind)
            current = config.model_display_name if config else f"{default_model_id} (default)"
            lines.append(f"• {bold(label)} — {escape_html(current)}")
            buttons.append([TelegramInlineButton(text=label, callback_data=f"modeltask:{task_kind.value}")])
        return UserFacingResponse(
            visibility=_PUBLIC, title="Model configuration", body="\n".join(lines), buttons=_with_nav_rows(buttons)
        )

    def model_list(self, task_kind: TaskKind, models: list[ModelCatalogEntry], current_model_id: str | None) -> UserFacingResponse:
        if not models:
            return UserFacingResponse(
                visibility=_PUBLIC,
                title=_TASK_KIND_LABELS[task_kind],
                body="No models were returned by Cursor's catalog just now.",
                buttons=_with_nav_rows([[TelegramInlineButton(text="Refresh", callback_data="modelrefresh")]]),
            )
        buttons = []
        for index, model in enumerate(models):
            mark = " \u2713" if model.id == current_model_id else ""
            buttons.append([TelegramInlineButton(text=f"{model.display_name}{mark}", callback_data=f"modelpick:{index}")])
        buttons.append([TelegramInlineButton(text="Refresh list", callback_data="modelrefresh")])
        buttons.append([TelegramInlineButton(text="Back", callback_data="modelback")])
        return UserFacingResponse(
            visibility=_PUBLIC,
            title=f"{_TASK_KIND_LABELS[task_kind]} — pick a model",
            body=f"{len(models)} model(s) available right now.",
            buttons=_with_nav_rows(buttons),
        )

    def model_detail(self, task_kind: TaskKind, model: ModelCatalogEntry) -> UserFacingResponse:
        buttons = []
        for index, variant in enumerate(model.variants):
            mark = " (default)" if variant.is_default else ""
            buttons.append([TelegramInlineButton(text=f"{variant.display_name}{mark}", callback_data=f"modelvariant:{index}")])
        if model.parameters:
            buttons.append([TelegramInlineButton(text="Customize parameters", callback_data="modelcustom")])
        buttons.append([TelegramInlineButton(text="Use with no extra parameters", callback_data="modeldefault")])
        buttons.append([TelegramInlineButton(text="Back", callback_data="modelback")])
        return UserFacingResponse(
            visibility=_PUBLIC,
            title=model.display_name,
            body=escape_html(model.description) or "No description provided.",
            buttons=_with_nav_rows(buttons),
        )

    def model_param_step(
        self, task_kind: TaskKind, model: ModelCatalogEntry, param: ModelParamDefinition, *, step_index: int, total_steps: int
    ) -> UserFacingResponse:
        buttons = [
            [TelegramInlineButton(text=value.display_name, callback_data=f"modelparam:{index}")]
            for index, value in enumerate(param.values)
        ]
        buttons.append([TelegramInlineButton(text="Back", callback_data="modelback")])
        return UserFacingResponse(
            visibility=_PUBLIC,
            title=f"{model.display_name} — step {step_index + 1} of {total_steps}",
            body=f"Choose a value for {bold(param.display_name)}.",
            buttons=_with_nav_rows(buttons),
        )

    def model_config_saved(self, task_kind: TaskKind, config: ModelConfig) -> UserFacingResponse:
        if config.params:
            params_text = ", ".join(f"{p.id}={p.value}" for p in config.params)
            body = f"{bold(_TASK_KIND_LABELS[task_kind])} will now use {bold(config.model_display_name)} ({escape_html(params_text)})."
        else:
            body = f"{bold(_TASK_KIND_LABELS[task_kind])} will now use {bold(config.model_display_name)} with its default settings."
        return UserFacingResponse(
            visibility=_PUBLIC,
            title="Model configuration saved",
            body=body,
            buttons=_with_nav_rows([[TelegramInlineButton(text="Configure another task", callback_data="modelmenu")]]),
        )
