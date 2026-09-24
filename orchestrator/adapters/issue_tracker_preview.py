"""The only IssueTrackerAdapter (orchestrator/adapters/protocols.py)
implementation that exists today. Produces labelled previews only --
CLAUDE.md §8: issue-tracker submission (Jira, ServiceNow, whichever the
audit team ends up on) is not built, and "Preview — not submitted" must
never be a fake successful integration (NN13). A real tracker becomes a
second implementation of the same Protocol; nothing that calls preview()
changes when that lands.
"""

from __future__ import annotations

from orchestrator.adapters.protocols import TicketPreview

TICKET_PREVIEW_STATUS = "Preview — not submitted"


class PreviewOnlyIssueTracker:
    def preview(self, issues: list[dict]) -> list[TicketPreview]:
        return [
            TicketPreview(
                issue_id=i["finding_id"],
                title=i["title"],
                severity=i["severity"],
                description=i.get("recommendation") or i.get("description"),
                status=TICKET_PREVIEW_STATUS,
            )
            for i in issues
        ]

    def submit(self, issues: list[dict]) -> list[dict]:
        raise NotImplementedError(
            "issue-tracker submission is not built (CLAUDE.md §8) -- preview() only"
        )
