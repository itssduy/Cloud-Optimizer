from datetime import datetime, timedelta, timezone

from botocore.exceptions import BotoCoreError, ClientError
from django.http import Http404
from django.shortcuts import render
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from . import auth, data

TEMPLATE = "cloud_optimizer/alerts.html"
CHECK_MESSAGES = {
    "normal": ("No anomaly detected", "Spending was within the alert threshold for the billing day shown."),
    "alert": ("Anomaly detected", "Review the saved alerts for the spending comparison."),
    "pending": ("Check pending", "Reload after the scheduled audit finishes."),
    "no_scan": ("No saved check", "Spending checks appear after the first audit."),
    "no_data": ("Billing data unavailable", "The audit is waiting for billing days from Cost Explorer."),
    "insufficient_history": ("Insufficient billing history", "More billing days are needed to establish a baseline."),
    "stale": ("Billing data is stale", "The latest available billing day is too old for a spending check."),
    "billing_failed": ("Billing fetch failed", "Ask the deployment operator to check Cost Explorer access."),
    "failed": ("Spending check failed", "Ask the deployment operator to check the audit logs."),
    "unavailable": ("Check result unavailable", "The audit has yet to save a spending check result."),
}
FINDINGS_SERVICES = {
    "Amazon Elastic Compute Cloud - Compute": "ec2",
    "Amazon EC2": "ec2",
    "Amazon Simple Storage Service": "s3",
}


@require_safe
@never_cache
def page(request, alert_id=None):
    try:
        return signed_in_page(request, alert_id)
    except (BotoCoreError, ClientError):
        message = "Saved alerts are temporarily unavailable. Reload to try again."
    except KeyError as error:
        if error.args not in (("AWS_ACCOUNT_ID",), ("DYNAMODB_TABLE_NAME",)):
            raise
        message = "Ask your deployment operator to check the app configuration."
    return render(request, TEMPLATE, {"me": getattr(request, "user_info", None), "alert_error": message}, status=503)


@auth.login_required
def signed_in_page(request, alert_id):
    status = data.status()
    scan = status["latest_scan"] or {}
    check = latest_check(scan)
    check_label, check_message = CHECK_MESSAGES.get(check["outcome"], CHECK_MESSAGES["unavailable"])
    checked_day = parse_date(check.get("date") or "")
    latest_billing_day = datetime.now(timezone.utc).date() - timedelta(days=1)
    context = {
        "me": request.user_info, "account_id": status["account_id"],
        "check": check, "check_label": check_label, "check_message": check_message,
        "checked_day": checked_day, "check_time": parse_datetime(scan.get("completed_at") or ""),
        "check_billed_at": parse_datetime(check.get("billing_fetched_at") or ""), "overdue": status["overdue"],
        "billing_delayed": checked_day and checked_day < latest_billing_day and check["outcome"] != "stale",
    }

    rows = []
    for record in data.alerts():
        if record.get("alert_type") != "spend_anomaly":
            continue
        day = parse_date(record.get("date") or "")
        row = {
            "record": record, "day": day,
            "detected_at": parse_datetime(record.get("created_at") or ""),
            "earlier": day and day < latest_billing_day,
        }
        if alert_id is None:
            rows.append(row)
        elif record["alert_id"] == alert_id:
            context.update({
                "alert": row, "billed_at": parse_datetime(record.get("billing_fetched_at") or ""),
                "contributors": [
                    {**item, "findings_service": FINDINGS_SERVICES.get(item["service"])}
                    for item in record.get("top_services") or []
                ],
            })
            return render(request, TEMPLATE, context)
    if alert_id is not None:
        raise Http404
    return render(request, TEMPLATE, {**context, "alerts": rows})


def latest_check(scan):
    if not scan:
        return {"outcome": "no_scan"}
    if scan.get("spending_check"):
        return scan["spending_check"]

    # Older audits saved errors and non-normal outcomes as notes.
    errors = {error.get("service") for error in (scan.get("errors") or [])}
    if "costs" in errors:
        return {"outcome": "billing_failed"}
    if "anomalies" in errors:
        return {"outcome": "failed"}
    if scan.get("status") in ("pending", "running"):
        return {"outcome": "pending"}
    for note in scan.get("notes") or []:
        for outcome in ("no_data", "insufficient_history", "stale"):
            if note.startswith(f"spending check: {outcome} ("):
                return {"outcome": outcome}
    return {"outcome": "unavailable"}
