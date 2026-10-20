"""Budget threshold alert evaluation (Phase 10, issue #471).

The ledger records usage; this module decides when a workspace's spend crosses
its budget limit. Alerts are deduplicated per workspace and period so a burst of
usage produces one notification instead of one per request. The in-process
deduplicator is intentionally small and resettable (tests and multi-process
workers each keep their own view); a persistent dedupe store can replace it
without changing the public :func:`check_budget_alerts` contract.

When SMTP is configured, over-budget alerts are also delivered by email through
the existing email service. Email delivery is best-effort and never raises; the
in-app notification remains the fallback and source of truth.
"""

from __future__ import annotations

import logging

from app.services.token_usage import BudgetAlertDeduplicator

logger = logging.getLogger(__name__)

#: Alert kind emitted when spend is above the configured limit.
ALERT_OVER_BUDGET = "over_budget"

_deduplicator = BudgetAlertDeduplicator()


def _send_alert_email(alert: dict) -> bool:
    """Best-effort email delivery of a budget alert.

    Returns `True` when the email was accepted by the transport and `False`
    otherwise (SMTP unconfigured, no recipient, delivery failure). Never
    raises: any failure is logged and swallowed so the in-app notification
    remains the fallback.
    """
    try:
        from flask import current_app

        recipient = current_app.config.get("BILLING_ALERT_EMAIL")
        if not recipient:
            logger.debug(
                "Budget alert has no recipient; skipping email for workspace %s",
                alert.get("workspace_id"),
            )
            return False

        from app.services.email import send_alert_email

        workspace_id = alert.get("workspace_id")
        period = alert.get("period") or ""
        spend = alert.get("spend")
        limit = alert.get("limit")
        over_by = alert.get("over_by")

        subject = f"Budget alert for workspace {workspace_id} ({period})"
        text_body = (
            f"Workspace {workspace_id} has exceeded its budget for period {period}.\n"
            f"Spend: {spend}\n"
            f"Limit: {limit}\n"
            f"Over budget by: {over_by}\n"
        )
        html_body = (
            "<p>Workspace <strong>"
            f"{workspace_id}</strong> has exceeded its budget for period "
            f"<strong>{period}</strong>.</p>"
            f"<p>Spend: {spend}<br />Limit: {limit}<br />"
            f"Over budget by: {over_by}</p>"
        )
        return send_alert_email(recipient, subject, text_body, html_body)
    except Exception as exc:  # pragma: no cover - email is best-effort
        logger.warning("Failed to deliver budget alert email: %s", exc)
        return False


def check_budget_alerts(workspace_id, *, spend, limit, period) -> list[dict]:
    """Return alerts for `workspace_id` for the given budget `period`.

    `spend` and `limit` are compared as numbers. A missing, zero or negative
    limit means no budget is configured, so no alert is emitted. The same alert
    (workspace + period + kind) fires at most once per period; starting a new
    period resets deduplication through
    :class:~class:~app.services.token_usage.BudgetAlertDeduplicator`.

    When SMTP is configured and a billing recipient is set, the alert is also
    emailed. Email failures never affect the returned alerts.
    """
    try:
        spend_value = float(spend or 0)
        limit_value = float(limit or 0)
    except (TypeError, ValueError):
        return []
    if limit_value <= 0 or spend_value <= limit_value:
        return []

    period_key = str(period or "")
    if not _deduplicator.check_and_mark((workspace_id, period_key), ALERT_OVER_BUDGET):
        return []
    alert = {
        "workspace_id": workspace_id,
        "kind": ALERT_OVER_BUDGET,
        "period": period_key,
        "spend": spend_value,
        "limit": limit_value,
        "over_by": round(spend_value - limit_value, 6),
    }
    _send_alert_email(alert)
    return [alert]


def reset() -> None:
    """Forget deduplication state (used between tests/periods)."""
    global _deduplicator
    _deduplicator = BudgetAlertDeduplicator()


##__ = ["ALERT_OVER_BUDGET", "check_budget_alerts", "reset"]
