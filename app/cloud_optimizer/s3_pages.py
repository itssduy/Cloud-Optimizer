import json
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from django.core import signing
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from . import auth, data, lifecycle

FINDING_TITLES = {
    "s3_incomplete_uploads": "Incomplete multipart uploads",
    "s3_tiering_candidate": "Intelligent-Tiering candidate",
}
REVIEW_TEMPLATE = "cloud_optimizer/s3_review.html"
REVIEW_SALT = "cloud-optimizer.s3-review"
REVIEW_ERRORS = {
    "not_found": "S3 could not locate the bucket in this deployment or allow access to its lifecycle settings.",
    "aws_unavailable": "S3 lifecycle settings are unavailable. Check AWS access and try reviewing the rule again.",
    "no_open_finding": "This finding has closed. Return to the bucket for its current findings.",
    "unsupported_rule": "This finding no longer has a supported lifecycle rule.",
    "stale_proposal": "The proposal or lifecycle configuration changed. Review the latest rule before confirming.",
    "already_applied": "S3 already has a lifecycle rule with this ID. Review the current configuration.",
    "bucket_busy": "Another lifecycle request is in progress for this bucket. Review the rule again after it finishes.",
    "not_confirmed": "Confirm the rule on its review screen before applying it.",
}


@require_http_methods(["GET", "HEAD", "POST"])
@never_cache
def page(request, bucket_name=None):
    try:
        return signed_in_page(request, bucket_name)
    except (BotoCoreError, ClientError):
        message = "Saved bucket data is unavailable. Ask your deployment operator to check dashboard access."
    except KeyError as error:
        if error.args not in (("AWS_ACCOUNT_ID",), ("DYNAMODB_TABLE_NAME",)):
            raise
        message = "Ask your deployment operator to check the app configuration."
    return render(request, "cloud_optimizer/s3_error.html", {
        "me": getattr(request, "user_info", None), "error_message": message,
    }, status=503)


@auth.login_required
def signed_in_page(request, bucket_name):
    saved = data.status()
    scan = saved["latest_scan"] or {}
    context = {
        "me": request.user_info, "account_id": saved["account_id"], "region": saved["region"],
        "scan": scan, "overdue": saved["overdue"],
    }
    if bucket_name is None:
        if request.method == "POST":
            return HttpResponseNotAllowed(["GET", "HEAD"])
        return inventory(request, context)
    try:
        result = data.resource("s3_bucket", bucket_name)
    except data.NotFound:
        raise Http404

    bucket = result["resource"]
    context["bucket"] = bucket
    if request.method == "POST":
        return confirm_change(request, context)
    if request.GET.get("request"):
        return outcome(request, context, request.GET["request"])

    findings = []
    for finding in result["findings"]:
        if finding["status"] != "open":
            continue
        evidence = finding.get("evidence") or {}
        recommendation = finding.get("recommendation") or {}
        findings.append({
            "title": FINDING_TITLES.get(finding["rule"], "Bucket finding"),
            "finding": finding,
            "detected_at": parse_datetime(finding.get("last_detected_at") or ""),
            "metrics_at": parse_datetime(evidence.get("metrics_at") or ""),
            "checked_at": parse_datetime(evidence.get("checked_at") or ""),
            "oldest_at": parse_datetime(evidence.get("oldest_initiated") or ""),
            "reviewable": finding["rule"] in FINDING_TITLES and bool(recommendation.get("proposed_rule")),
        })

    context.update({
        "findings": findings,
        "observed_at": parse_datetime(bucket.get("last_seen_at") or ""),
        "metrics_at": parse_datetime(bucket.get("details", {}).get("metrics_at") or ""),
        "older_observation": bool(scan and bucket.get("last_scan_id") != scan.get("scan_id")),
    })
    review = request.GET.get("review")
    if review:
        for row in findings:
            if row["reviewable"] and row["finding"]["rule"] == review:
                context.update(row)
                return review_rule(request, context, review)
        raise Http404
    recent = [
        record for record in result["actions"]
        if record["action_type"] == "s3_apply_lifecycle" and record["target"] == lifecycle.target(bucket_name)
    ]
    for record in recent[:5]:
        record["requested_at"] = parse_datetime(record["requested_at"])
        record["title"] = FINDING_TITLES.get(record["rule"], "Lifecycle rule")
    context["recent_actions"] = recent[:5]
    return render(request, "cloud_optimizer/s3_detail.html", context)


def review_scope(context):
    return {
        "user_id": context["me"]["user_id"], "account_id": context["account_id"],
        "region": context["region"], "bucket": context["bucket"]["resource_id"],
    }


def review_rule(request, context, rule):
    bucket = context["bucket"]["resource_id"]
    proposal, status = lifecycle.preview(bucket, rule)
    if status != 200:
        context["review_error"] = REVIEW_ERRORS[proposal["error"]]
    else:
        context["proposal"] = proposal
        context["rule_json"] = json.dumps(proposal["proposed_rule"], indent=2)
        if not proposal["already_applied"]:
            approval = {"scope": review_scope(context), "rule": rule,
                        "proposal_hash": proposal["proposal_hash"], "request_id": str(uuid.uuid4())}
            context["confirmation"] = signing.dumps(approval, salt=REVIEW_SALT)
    return render(request, REVIEW_TEMPLATE, context, status=status)


def confirm_change(request, context):
    try:
        approval = signing.loads(request.POST.get("confirmation", ""), salt=REVIEW_SALT)
    except signing.BadSignature:
        approval = {}
    if approval.get("scope") != review_scope(context):
        context["review_error"] = "Open a review for this bucket before confirming the change."
        return render(request, REVIEW_TEMPLATE, context, status=400)

    bucket = context["bucket"]["resource_id"]
    rule = approval["rule"]
    request_id = approval["request_id"]
    confirm = request.POST.get("confirm")
    detail_url = reverse("s3_detail", args=[bucket])
    if confirm == "yes" and lifecycle.saved_action(bucket, request_id):
        return redirect(f"{detail_url}?request={request_id}")

    result, status = lifecycle.apply(bucket, context["me"], rule, confirm, request_id, approval["proposal_hash"])
    if result.get("action") or result.get("error") == "duplicate_request":
        return redirect(f"{detail_url}?request={request_id}")

    error = result["error"]
    if error in ("bucket_busy", "stale_proposal", "not_confirmed", "aws_unavailable"):
        context["retry_rule"] = rule
    context["review_error"] = REVIEW_ERRORS.get(error, "The lifecycle request could not be submitted.")
    return render(request, REVIEW_TEMPLATE, context, status=status)


def outcome(request, context, request_id):
    record = lifecycle.check_action(context["bucket"]["resource_id"], request_id)
    if not record:
        raise Http404
    context.update({
        "action_record": record, "title": FINDING_TITLES.get(record["rule"], "Lifecycle rule"),
        "requested_at": parse_datetime(record["requested_at"]),
        "checked_at": parse_datetime(record["outcome_checked_at"] or ""),
        "access_denied": record["request_error"] in ("AccessDenied", "AccessDeniedException", "403"),
    })
    return render(request, REVIEW_TEMPLATE, context)


def inventory(request, context):
    buckets = data.resources("s3_bucket")
    ids_with_findings = set()
    for finding in data.findings():
        if finding["resource_type"] == "s3_bucket":
            ids_with_findings.add(finding["resource_id"])

    finding_filter = request.GET.get("finding", "")
    rows = []
    for bucket in buckets:
        has_findings = bucket["resource_id"] in ids_with_findings
        if finding_filter == "open" and not has_findings:
            continue
        if finding_filter == "none" and has_findings:
            continue
        rows.append({"bucket": bucket, "has_findings": has_findings})
    context.update({"rows": rows, "has_inventory": bool(buckets), "finding_filter": finding_filter})
    return render(request, "cloud_optimizer/s3_inventory.html", context)
