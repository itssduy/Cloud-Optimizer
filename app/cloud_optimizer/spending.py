from datetime import datetime, timedelta, timezone
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError
from django.shortcuts import render
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from . import auth, data
from .dashboard import finding_rows

TEMPLATE = "cloud_optimizer/spending.html"


@require_safe
@never_cache
def page(request):
    try:
        return signed_in_page(request)
    except (BotoCoreError, ClientError):
        message = "Saved spending data is temporarily unavailable. Reload to try again."
    except KeyError as error:
        if error.args not in (("AWS_ACCOUNT_ID",), ("DYNAMODB_TABLE_NAME",)):
            raise
        message = "Ask your deployment operator to check the app configuration."
    return render(request, TEMPLATE, {
        "me": getattr(request, "user_info", None), "spending_error": message,
    }, status=503)


@auth.login_required
def signed_in_page(request):
    days = 7 if request.GET.get("days") == "7" else 30
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    end = today - timedelta(days=1)
    records = [record for record in data.costs() if record["date"] <= end.isoformat()]
    services = sorted({service for record in records for service in (record.get("by_service") or {})})
    service = request.GET.get("service", "")
    if service not in services:
        service = ""
    period = [record for record in records if record["date"] >= start.isoformat()]
    rows = daily_rows(period, start, days, service)
    observed = [row for row in rows if row["amount"] is not None]

    savings = data.savings() or {}
    monthly = savings.get("total_monthly")
    if savings.get("finding_count") and savings.get("finding_count") == savings.get("missing_estimate_count"):
        monthly = None
    scans = data.scans(limit=1)
    scan = scans[0] if scans else {}
    billing_error = any(error.get("service") == "costs" for error in (scan.get("errors") or []))
    fetched_at = max((record.get("fetched_at") or "" for record in period), default="")
    findings = data.findings()
    savings_findings = finding_rows(data.resources(), findings, "all") if findings else []

    return render(request, TEMPLATE, {
        "me": request.user_info, "account_id": data.account_id(),
        "days": days, "start": start, "end": end, "service": service, "services": services,
        "rows": rows, "day_count": len(observed), "billing_day_count": len(period),
        "total": sum(row["amount"] for row in observed) if observed else None,
        "currency": observed[0]["currency"] if observed else None,
        "has_estimates": any(row["status"] == "Estimated" for row in observed),
        "has_credits": any(row["amount"] < 0 for row in observed),
        "fetched_at": parse_datetime(fetched_at), "billing_error": billing_error,
        "breakdown": service_breakdown(period), "monthly": monthly, "savings": savings,
        "calculated_at": parse_datetime(savings.get("calculated_at") or ""),
        "savings_findings": savings_findings,
    })


def daily_rows(records, start, days, service):
    by_date = {record["date"]: record for record in records}
    rows = []
    for offset in range(days):
        date = start + timedelta(days=offset)
        record = by_date.get(date.isoformat(), {})
        money = record.get("total") or {}
        if service:
            money = (record.get("by_service") or {}).get(service) or {}
        amount = money.get("amount")
        status = "Unavailable"
        if amount is not None:
            amount = Decimal(str(amount))
            status = "Estimated" if money.get("estimated") else "Reported"
        elif record and service and not money:
            status = "No service entry"
        rows.append({"date": date, "amount": amount, "currency": money.get("currency"), "status": status})

    peak = max((abs(row["amount"]) for row in rows if row["amount"] is not None), default=0)
    for row in rows:
        if peak and row["amount"]:
            row["bar_height"] = max(1, round(abs(row["amount"]) * 100 / peak))
    return rows


def service_breakdown(records):
    totals = {}
    for record in records:
        for service, money in (record.get("by_service") or {}).items():
            if not money or money.get("amount") is None:
                continue
            if service not in totals:
                totals[service] = {"service": service, "amount": Decimal("0"), "currency": money.get("currency")}
            bill = totals[service]
            bill["amount"] += Decimal(str(money["amount"]))
            if money.get("estimated"):
                bill["estimated"] = True
    return sorted(totals.values(), key=lambda bill: bill["amount"], reverse=True)
