from botocore.exceptions import BotoCoreError, ClientError
from django.shortcuts import render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from . import auth, data, users
from .actions import REQUEST_ID
from .dashboard import RESOURCE_PAGES, SERVICE_NAMES

TEMPLATE = "cloud_optimizer/activity.html"
HISTORY_LIMIT = 50
ACTIONS = {
    "ec2_start": "Start instance",
    "ec2_stop": "Stop instance",
    "s3_incomplete_uploads": "Clean up incomplete uploads",
    "s3_tiering_candidate": "Enable Intelligent-Tiering",
    "s3_apply_lifecycle": "Apply lifecycle rule",
}
STATUSES = {
    "pending": "Pending",
    "verified": "Verified",
    "denied": "Denied",
    "failed": "Failed",
    "unavailable": "Unavailable",
}
DENIALS = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "403")
FAILURES = {
    "IncorrectInstanceState": "The instance changed state before AWS could apply the request.",
    "InvalidInstanceID.NotFound": "AWS could not find the instance.",
    "NoSuchBucket": "AWS could not find the bucket.",
}


@require_safe
@never_cache
def page(request):
    try:
        return signed_in_page(request)
    except (BotoCoreError, ClientError):
        message = "Saved activity is temporarily unavailable. Reload to try again."
    except KeyError as error:
        if error.args not in (("AWS_ACCOUNT_ID",), ("DYNAMODB_TABLE_NAME",)):
            raise
        message = "Ask your deployment operator to check the app configuration."
    return render(request, TEMPLATE, {"me": getattr(request, "user_info", None), "read_error": message}, status=503)


@auth.login_required
def signed_in_page(request):
    account_id = data.account_id()
    records = data.actions(limit=HISTORY_LIMIT)
    selected = {
        "service": request.GET.get("service", ""),
        "resource": request.GET.get("resource", "").strip(),
        "action": request.GET.get("action", ""),
        "status": request.GET.get("status", ""),
    }
    inventory = {}
    people = {}
    if records:
        for resource in data.resources():
            inventory[resource["resource_type"], resource["resource_id"]] = resource.get("name") or resource["resource_id"]
        people = {user["user_id"]: user["email"] for user in users.list_users()}

    rows = []
    resource_search = selected["resource"].casefold()
    for record in records:
        target = record.get("target") or {}
        resource_type = target.get("resource_type")
        resource_id = target.get("resource_id") or ""
        name = inventory.get((resource_type, resource_id))
        action = record.get("action_type")
        if action == "s3_apply_lifecycle" and record.get("rule") in ACTIONS:
            action = record["rule"]
        status, explanation = saved_result(record, resource_type)
        service = SERVICE_NAMES.get(resource_type, "Unknown service")
        if selected["service"] and selected["service"] != service.lower():
            continue
        if resource_search and resource_search not in resource_id.casefold() and resource_search not in (name or "").casefold():
            continue
        if selected["action"] and selected["action"] not in (action, record.get("action_type")):
            continue
        if selected["status"] and selected["status"] != status:
            continue

        change = record.get("change") or {}
        before = change.get("before") or {}
        after = change.get("after") or {}
        rules = after.get("rules") or []
        rule = rules[-1] if rules else {}
        rule_filter = rule.get("Filter") or {}
        cleanup = rule.get("AbortIncompleteMultipartUpload") or {}
        transitions = rule.get("Transitions") or []
        transition = transitions[0] if transitions else {}
        actor = record.get("requested_by")
        resource_url = ""
        if name and resource_type in RESOURCE_PAGES and data.RESOURCE_ID.fullmatch(resource_id):
            resource_url = reverse(RESOURCE_PAGES[resource_type], args=[resource_id])

        request_id = record.get("action_id") or ""
        rows.append({
            "resource": name or resource_id, "resource_id": resource_id, "resource_url": resource_url,
            "service": service,
            "action": ACTIONS.get(action, "Action unavailable"), "status": STATUSES[status], "explanation": explanation,
            "actor": people.get(actor) or actor, "requested_at": saved_time(record.get("requested_at")),
            "checked_at": saved_time(record.get("outcome_checked_at")),
            "before_state": before.get("state"), "after_state": after.get("state"),
            "observed_state": record.get("observed_state"),
            "rule_id": rule.get("ID"), "cleanup_days": cleanup.get("DaysAfterInitiation"),
            "transition_days": transition.get("Days"), "minimum_bytes": rule_filter.get("ObjectSizeGreaterThan"),
            "request_id": request_id if status != "unavailable" and REQUEST_ID.fullmatch(request_id) else "",
        })

    return render(request, TEMPLATE, {
        "me": request.user_info, "account_id": account_id, "region": data.region(),
        "rows": rows, "selected": selected, "has_records": bool(records), "history_limit": HISTORY_LIMIT,
        "action_labels": ACTIONS, "status_labels": STATUSES,
    })


def saved_result(record, resource_type):
    request_status = record.get("request_status")
    outcome = record.get("outcome")
    error = record.get("request_error")
    if error in DENIALS or request_status == "denied":
        return "denied", "AWS denied access to this resource or action."
    if request_status == "failed" or outcome == "failed":
        return "failed", FAILURES.get(error, "The request failed. No change was verified.")
    if outcome == "verified":
        if resource_type == "s3_bucket":
            return "verified", "S3 confirmed the rule was saved. S3 processes eligible objects and uploads later."
        if record.get("observed_state"):
            return "verified", "EC2 reported the requested state at the last check."
        return "verified", "The saved result is verified. The observed state is unavailable."
    if request_status == "sent":
        return "pending", "AWS accepted the request. The outcome is awaiting verification."
    if request_status == "pending":
        return "pending", "The request was recorded. AWS acceptance is unavailable."
    return "unavailable", "The saved record has no outcome."


def saved_time(value):
    try:
        return parse_datetime(value or "")
    except (TypeError, ValueError):
        return None
