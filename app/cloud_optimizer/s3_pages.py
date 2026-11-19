import json

from botocore.exceptions import BotoCoreError, ClientError
from django.http import Http404
from django.shortcuts import render
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from . import auth, data

FINDING_TITLES = {
    "s3_incomplete_uploads": "Incomplete multipart uploads",
    "s3_tiering_candidate": "Intelligent-Tiering candidate",
}


@require_safe
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
        return inventory(request, context)
    try:
        result = data.resource("s3_bucket", bucket_name)
    except data.NotFound:
        raise Http404

    bucket = result["resource"]
    findings = []
    for finding in result["findings"]:
        if finding["status"] != "open":
            continue
        findings.append({
            "title": FINDING_TITLES.get(finding["rule"], "Bucket finding"),
            "finding": finding,
            "detected_at": parse_datetime(finding.get("last_detected_at") or ""),
            "metrics_at": parse_datetime(finding.get("evidence", {}).get("metrics_at") or ""),
            "checked_at": parse_datetime(finding.get("evidence", {}).get("checked_at") or ""),
            "oldest_at": parse_datetime(finding.get("evidence", {}).get("oldest_initiated") or ""),
            "reviewable": finding["rule"] in FINDING_TITLES and bool(finding.get("recommendation", {}).get("proposed_rule")),
        })

    context.update({
        "bucket": bucket, "findings": findings,
        "observed_at": parse_datetime(bucket.get("last_seen_at") or ""),
        "metrics_at": parse_datetime(bucket.get("details", {}).get("metrics_at") or ""),
        "older_observation": bool(scan and bucket.get("last_scan_id") != scan.get("scan_id")),
    })
    review = request.GET.get("review")
    if review:
        for row in findings:
            if row["reviewable"] and row["finding"]["rule"] == review:
                context.update(row)
                context["rule_json"] = json.dumps(row["finding"]["recommendation"]["proposed_rule"], indent=2)
                return render(request, "cloud_optimizer/s3_review.html", context)
        raise Http404
    return render(request, "cloud_optimizer/s3_detail.html", context)


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
