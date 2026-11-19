import uuid

from botocore.exceptions import BotoCoreError, ClientError
from django.core import signing
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from . import actions, auth, data

REVIEW_SALT = "cloud-optimizer.ec2-review"
ACTION_TEMPLATE = "cloud_optimizer/ec2_action.html"
ACTION_ERRORS = {
    "wrong_state": "The instance changed state after review. Return to the instance and check its current state.",
    "not_allowed": "This instance is ineligible for the requested action.",
    "not_found": "AWS could not find the instance in this deployment.",
    "aws_unavailable": "The dashboard could not check the instance in EC2. Try again after checking AWS access.",
    "not_confirmed": "Review and confirm the change before submitting it.",
}


@require_http_methods(["GET", "HEAD", "POST"])
@never_cache
def page(request, instance_id=None):
    try:
        return signed_in_page(request, instance_id)
    except (BotoCoreError, ClientError):
        return error_page(request, "EC2 data is unavailable. Check the instance state before retrying a change.", 503)
    except KeyError as error:
        if error.args not in (("AWS_ACCOUNT_ID",), ("DYNAMODB_TABLE_NAME",)):
            raise
        return error_page(request, "Ask your deployment operator to check the app configuration.", 503)


@auth.login_required
def signed_in_page(request, instance_id):
    context = {"me": request.user_info, "account_id": data.account_id(), "region": data.region()}
    if instance_id is None:
        if request.method == "POST":
            return HttpResponseNotAllowed(["GET", "HEAD"])
        return inventory(request, context)

    if not actions.INSTANCE_ID.fullmatch(instance_id):
        return error_page(request, "Instance unavailable in this deployment.", 404)
    try:
        context.update(data.resource("ec2_instance", instance_id))
    except data.NotFound:
        return error_page(request, "Instance unavailable in this deployment.", 404)
    context["instance"] = context.pop("resource")

    if request.method == "POST":
        return confirm_change(request, context)
    request_id = request.GET.get("request")
    if request_id:
        result, status = actions.check_action(request_id, instance_id)
        record = result.get("action") or {}
        context.update({
            "request_id": request_id,
            "action_record": record,
            "checked_at": parse_datetime(record.get("outcome_checked_at") or ""),
            "access_denied": record.get("request_error") in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"),
        })
        return render(request, ACTION_TEMPLATE, context, status=status)

    context.update(current_controls(instance_id))
    review = request.GET.get("review")
    if review and review == context.get("available_action"):
        # Bind the review to one user, account, region, instance and request ID.
        confirmation = {"scope": review_scope(context), "action": review, "request_id": str(uuid.uuid4())}
        context.update({"review": review, "confirmation": signing.dumps(confirmation, salt=REVIEW_SALT)})
        return render(request, ACTION_TEMPLATE, context)
    if review:
        context["review_error"] = "This change is unavailable. Review the current instance state."
    return detail(request, context)


def scan_notes():
    status = data.status()
    notes = []
    if status["scan_state"] == "no_scan":
        notes.append("No audit is saved for this region yet.")
    if status["scan_state"] in ("partial", "failed"):
        notes.append("The latest audit did not finish all checks. Some saved results may be older.")
    if status["overdue"]:
        notes.append("The daily audit is overdue. Saved instance observations may be stale.")
    return notes


def inventory(request, context):
    instances = data.resources("ec2_instance")
    ids_with_findings = set()
    for finding in data.findings():
        if finding["resource_type"] == "ec2_instance":
            ids_with_findings.add(finding["resource_id"])

    state_filter = request.GET.get("state", "")
    finding_filter = request.GET.get("finding", "")
    states = {instance["state"] for instance in instances}
    if state_filter:
        states.add(state_filter)
    rows = []
    for instance in instances:
        has_findings = instance["resource_id"] in ids_with_findings
        if state_filter and instance["state"] != state_filter:
            continue
        if finding_filter == "open" and not has_findings:
            continue
        if finding_filter == "none" and has_findings:
            continue
        rows.append({"instance": instance, "has_findings": has_findings})

    context.update({
        "rows": rows, "states": sorted(states), "state_filter": state_filter,
        "finding_filter": finding_filter, "has_inventory": bool(instances), "scan_notes": scan_notes(),
    })
    return render(request, "cloud_optimizer/ec2_inventory.html", context)


def detail(request, context):
    instance = context["instance"]
    findings = [finding for finding in context["findings"] if finding["status"] == "open"]
    evidence = {}
    for finding in findings:
        if finding["rule"] == "ec2_idle_cpu":
            evidence = finding.get("evidence") or {}
            break
    recent = []
    for record in context["actions"]:
        target = record["target"]
        if target["resource_type"] != "ec2_instance" or target["region"] != context["region"]:
            continue
        status = "Accepted"
        if record["outcome"] == "verified":
            status = f"Confirmed {record['observed_state']}"
        elif record["outcome"] == "failed":
            status = "Failed"
        request_id = record["action_id"]
        recent.append({
            "label": "Stop" if record["action_type"] == "ec2_stop" else "Start",
            "requested_at": parse_datetime(record.get("requested_at") or ""),
            "status": status,
            "request_id": request_id,
            "checkable": bool(actions.REQUEST_ID.fullmatch(request_id)),
        })
        if len(recent) == 5:
            break

    context.update({
        "findings": findings, "evidence": evidence, "scan_notes": scan_notes(), "recent_actions": recent,
        "observed_at": parse_datetime(instance.get("last_seen_at") or ""),
        "window_start": parse_datetime(evidence.get("window_start") or ""),
        "window_end": parse_datetime(evidence.get("window_end") or ""),
    })
    return render(request, "cloud_optimizer/ec2_detail.html", context)


def current_controls(instance_id):
    try:
        instance = actions.describe(instance_id)
    except (BotoCoreError, ClientError):
        return {"action_note": "Current EC2 state is unavailable. Ask your deployment operator to check EC2 access."}
    if not instance:
        return {"action_note": "EC2 could not find this instance in the deployment account and region."}

    state = instance["State"]["Name"]
    reason = actions.blocked_reason(instance)
    if reason:
        return {"live_state": state, "action_note": f"Actions unavailable ({reason})."}
    for action, (required_state, _) in actions.ACTIONS.items():
        if state == required_state:
            return {"live_state": state, "available_action": action}
    return {"live_state": state, "action_note": "Wait for the instance state to settle before requesting a change."}


def review_scope(context):
    return {
        "user_id": context["me"]["user_id"],
        "account_id": context["account_id"],
        "region": context["region"],
        "instance_id": context["instance"]["resource_id"],
    }


def confirm_change(request, context):
    try:
        approval = signing.loads(request.POST.get("confirmation", ""), salt=REVIEW_SALT)
    except signing.BadSignature:
        approval = {}
    if approval.get("scope") != review_scope(context):
        return error_page(request, "Open a review for this instance before confirming the change.", 400)

    instance_id = context["instance"]["resource_id"]
    request_id = approval["request_id"]
    result, status = actions.submit_action(
        context["me"], instance_id, approval["action"], request.POST.get("confirm"), request_id
    )
    if result.get("action") or result.get("error") == "duplicate_request":
        url = reverse("ec2_detail", args=[instance_id])
        return redirect(f"{url}?request={request_id}")

    context["request_error"] = ACTION_ERRORS.get(result.get("error"), "The EC2 request could not be submitted.")
    return render(request, ACTION_TEMPLATE, context, status=status)


def error_page(request, message, status):
    return render(request, "cloud_optimizer/ec2_error.html", {
        "me": getattr(request, "user_info", None), "error_message": message,
    }, status=status)
