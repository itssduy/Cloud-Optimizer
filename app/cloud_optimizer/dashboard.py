import json
import os
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from . import auth, data

TEMPLATE = "cloud_optimizer/dashboard.html"
STATUS_TEMPLATE = "cloud_optimizer/deployment_status.html"
FINDINGS_TEMPLATE = "cloud_optimizer/findings.html"
SERVICE_NAMES = {"ec2_instance": "EC2", "s3_bucket": "S3"}
RESOURCE_PAGES = {"ec2_instance": "ec2_detail", "s3_bucket": "s3_detail"}
SCAN_STATES = {
    "no_scan": ("No saved scan", "Reload after the scheduled audit runs."),
    "pending": ("Pending", "The saved status shows the audit is waiting to start."),
    "running": ("Running", "The latest update shows the audit started. Reload to check for results."),
    "completed": ("Completed", "The latest scan finished and saved its results."),
    "partial": ("Partial", "Some checks did not finish. Ask the deployment operator to review the audit logs."),
    "failed": ("Failed", "Ask the deployment operator to review the audit Lambda logs."),
    "unavailable": ("Unavailable", "The scan state could not be determined from the saved data."),
}


def overview(request):
    # The home view checks the user's session before calling this function.
    context = {"me": request.user_info}
    if not os.environ.get("AWS_ACCOUNT_ID"):
        context["data_error"] = "Ask your administrator to configure the deployment account."
        return render(request, TEMPLATE, context)

    try:
        status = data.status()
        costs = data.costs()  # Billing days are returned oldest first.
        savings = data.savings()
        resources = data.resources()
        findings = data.findings()
        alerts = data.alerts(status="open")
    except (BotoCoreError, ClientError):
        context["data_error"] = "Saved dashboard results are temporarily unavailable."
        return render(request, TEMPLATE, context, status=503)

    context.update({
        "account_id": status["account_id"],
        "region": status["region"],
        "scan": status["latest_scan"],
        "overdue": status["overdue"],
        "spending": costs[-1] if costs else None,
        "savings": savings,
    })
    return render_dashboard(request, context, resources, findings, alerts)


@require_safe
@never_cache
def deployment_status(request):
    # The same store holds team accounts and scan results.
    try:
        return signed_in_status(request)
    except (BotoCoreError, ClientError):
        message = "Saved status is unavailable. Ask the deployment operator to check the app's DynamoDB access."
    except KeyError as error:
        if error.args != ("DYNAMODB_TABLE_NAME",):
            raise
        message = "The app's DynamoDB table is missing from its configuration. Ask the deployment operator to check the Terraform settings."
    return render(request, STATUS_TEMPLATE, {
        "me": getattr(request, "user_info", None),
        "status_error": message,
    }, status=503)


@auth.login_required
def signed_in_status(request):
    context = {"me": request.user_info}
    if not os.environ.get("AWS_ACCOUNT_ID"):
        context["status_error"] = "The AWS account is missing from the app configuration. Ask the deployment operator to check the Terraform account output."
        return render(request, STATUS_TEMPLATE, context, status=503)

    saved = data.status()
    deployment = saved["deployment"] or {}
    scan = saved["latest_scan"] or {}
    region = saved["region"]
    # Only use a saved readiness report for this installation's account and region.
    if deployment.get("account_id") != saved["account_id"] or region not in deployment.get("regions", []):
        deployment = {}

    scan_state = saved["scan_state"]
    scan_label, scan_message = SCAN_STATES.get(scan_state, SCAN_STATES["unavailable"])
    permission_issue = False
    for error in scan.get("errors", []):
        code = error.get("message", "").split(":", 1)[0]
        if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "403"):
            permission_issue = True
            break

    context.update({
        "account_id": saved["account_id"],
        "regions": deployment.get("regions", [region]),
        "readiness": deployment.get("status"),
        "scan_state": scan_state,
        "scan_label": scan_label,
        "scan_message": scan_message,
        "scan": scan,
        "started_at": parse_datetime(scan.get("started_at") or ""),
        "completed_at": parse_datetime(scan.get("completed_at") or ""),
        "overdue": saved["overdue"],
        "permission_issue": permission_issue,
    })
    return render(request, STATUS_TEMPLATE, context)


@require_safe
@never_cache
def findings_page(request):
    try:
        return signed_in_findings(request)
    except (BotoCoreError, ClientError):
        message = "Saved findings are temporarily unavailable. Reload to try again."
    except KeyError as error:
        if error.args not in (("AWS_ACCOUNT_ID",), ("DYNAMODB_TABLE_NAME",)):
            raise
        message = "Ask your deployment operator to check the app configuration."
    return render(request, FINDINGS_TEMPLATE, {
        "me": getattr(request, "user_info", None), "findings_error": message,
    }, status=503)


@auth.login_required
def signed_in_findings(request):
    service = request.GET.get("service", "all")
    if service not in ("all", "ec2", "s3"):
        service = "all"
    finding_status = request.GET.get("status", "open")
    if finding_status not in ("open", "resolved"):
        finding_status = "open"

    status = data.status()
    scan = status["latest_scan"] or {}
    scan_label, scan_message = SCAN_STATES.get(status["scan_state"], SCAN_STATES["unavailable"])
    rows = finding_rows(data.resources(), data.findings(status=finding_status), service)
    rows.sort(key=lambda row: row["finding"].get("last_detected_at") or "", reverse=True)
    for row in rows:
        row["seen_at"] = parse_datetime(row["finding"].get("last_detected_at") or "")
        row["resolved_at"] = parse_datetime(row["finding"].get("resolved_at") or "")

    return render(request, FINDINGS_TEMPLATE, {
        "me": request.user_info, "account_id": status["account_id"], "region": status["region"],
        "scan": scan, "scan_state": status["scan_state"], "scan_label": scan_label,
        "scan_message": scan_message, "overdue": status["overdue"],
        "scan_finished_at": parse_datetime(scan.get("completed_at") or ""),
        "scan_started_at": parse_datetime(scan.get("started_at") or ""),
        "service": service, "finding_status": finding_status, "rows": rows,
    })


def read_example(name):
    path = Path(__file__).parent / "examples" / (name + ".json")
    return json.loads(path.read_text())


def preview(request):
    if not settings.DEBUG:
        raise Http404

    scan = read_example("scan_completed") or {}
    context = {
        "sample": True,
        "account_id": scan.get("account_id"),
        "region": ", ".join(scan.get("regions", [])),
        "scan": scan,
        "spending": read_example("daily_cost"),
        "savings": read_example("savings"),
    }
    resources = []
    findings = []
    if scan:
        resources = [read_example("resource_ec2"), read_example("resource_s3")]
        findings = [read_example("finding_ec2_idle"), read_example("finding_s3_tiering")]
    alert = read_example("alert")
    alerts = [alert] if alert else []
    return render_dashboard(request, context, resources, findings, alerts)


def finding_rows(resources, findings, service):
    # Look up each resource name. A finding can outlast its inventory record.
    names = {}
    for resource in resources:
        key = (resource["resource_type"], resource["region"], resource["resource_id"])
        names[key] = resource.get("name")

    rows = []
    for finding in findings:
        key = (finding["resource_type"], finding["region"], finding["resource_id"])
        label = SERVICE_NAMES.get(finding["resource_type"], finding["resource_type"])
        if service != "all" and label.lower() != service:
            continue
        page = RESOURCE_PAGES.get(finding["resource_type"])
        resource_url = reverse(page, args=[finding["resource_id"]]) if page and key in names else ""
        rows.append({
            "name": names.get(key) or finding["resource_id"],
            "resource_url": resource_url,
            "service": label,
            "finding": finding,
        })
    return rows


def render_dashboard(request, context, resources, findings, alerts):
    service = request.GET.get("service", "all")
    if service not in ("all", "ec2", "s3"):
        service = "all"
    rows = finding_rows(resources, findings, service)
    scan = context.get("scan")
    if scan and scan.get("completed_at"):
        context["scan_time"] = parse_datetime(scan["completed_at"])

    spending = context.get("spending")
    service_costs = []
    if spending and spending["total"]["amount"] is not None:
        spending["date"] = parse_date(spending["date"])
        total = spending["total"]["amount"]
        for name, cost in spending["by_service"].items():
            amount = cost["amount"]
            if amount == 0:
                continue
            percent = 0
            if total > 0 and amount is not None and amount > 0:
                percent = min(100, round(amount / total * 100))
            service_costs.append({"name": name, "amount": amount, "percent": percent})
        service_costs.sort(key=lambda c: -(c["amount"] or 0))

    alert = alerts[0] if alerts else None
    if alert:
        alert["date"] = parse_date(alert["date"])

    context.update({
        "service": service,
        "rows": rows,
        "finding_count": len(findings),
        "alert": alert,
        "alert_count": len(alerts),
        "service_costs": service_costs,
    })
    return render(request, TEMPLATE, context)
