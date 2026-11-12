from datetime import date, timedelta
from decimal import Decimal

# A day is an anomaly when spend is more than PERCENT above the average of the
# BASELINE_DAYS before it, and at least MIN_INCREASE dollars more.
BASELINE_DAYS = 14
MIN_HISTORY_DAYS = 7
PERCENT = 50
MIN_INCREASE = Decimal("1.00")
STALE_AFTER_DAYS = 3
ALERT_TTL_DAYS = 400


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def money(amount, estimated=False):
    return {"amount": Decimal(amount).quantize(Decimal("0.01")), "currency": "USD", "estimated": estimated}


def average(values):
    return sum(values, Decimal("0")) / len(values)


def check_spending(account_id, daily_costs, now, baseline_days=BASELINE_DAYS,
                   min_history=MIN_HISTORY_DAYS, percent=PERCENT, min_increase=MIN_INCREASE):
    """Looks at the most recent day of spend from #26's daily cost items.

    Returns {"outcome", "date", "note", "alert"} where outcome is one of
    "alert", "normal", "insufficient_history", "stale", or "no_data".
    The alert key is the date, so running this twice doesn't make two alerts.
    """
    if not daily_costs:
        return {"outcome": "no_data", "date": None, "note": "no billing data yet", "alert": None}

    by_date = {c["date"]: c for c in daily_costs}
    latest = by_date[max(by_date)]
    day = date.fromisoformat(latest["date"])
    result = {"outcome": None, "date": latest["date"], "note": None, "alert": None}

    if (now.date() - day).days > STALE_AFTER_DAYS:
        result["outcome"] = "stale"
        result["note"] = f"newest billing data is from {latest['date']}"
        return result

    window = [(day - timedelta(days=i)).isoformat() for i in range(1, baseline_days + 1)]
    baseline = [by_date[d] for d in window if d in by_date]
    if len(baseline) < min_history:
        result["outcome"] = "insufficient_history"
        result["note"] = f"only {len(baseline)} of {min_history} days of history needed"
        return result

    actual = latest["total"]["amount"]
    expected = average([c["total"]["amount"] for c in baseline])
    increase = actual - expected
    if increase < min_increase or actual <= expected * (1 + Decimal(percent) / 100):
        result["outcome"] = "normal"
        return result

    services = set(latest["by_service"]) | {s for c in baseline for s in c["by_service"]}
    changes = []
    for service in services:
        now_amount = latest["by_service"].get(service, {}).get("amount", Decimal("0"))
        before = average([c["by_service"].get(service, {}).get("amount", Decimal("0")) for c in baseline])
        changes.append((now_amount - before, service, now_amount, before))
    changes.sort(reverse=True)
    top = [{"service": s, "actual": money(a, latest["total"]["estimated"]), "expected": money(b)}
           for diff, s, a, b in changes[:3] if diff > 0]

    pct = round(increase / expected * 100) if expected else None
    estimated = latest["total"]["estimated"]
    explanation = (f"Spend on {latest['date']} was ${actual:.2f}, "
                   + (f"{pct}% above" if pct is not None else "up from")
                   + f" the {len(baseline)}-day average of ${expected:.2f}")
    if top:
        explanation += f", mostly from {top[0]['service']}"
    explanation += "."
    if estimated:
        explanation += " This day is still an estimate from Cost Explorer and may change."

    result["outcome"] = "alert"
    result["alert"] = {
        "pk": f"ACCT#{account_id}",
        "sk": f"ALERT#{latest['date']}#total",
        "type": "alert",
        "account_id": account_id,
        "alert_id": f"{latest['date']}-total",
        "alert_type": "spend_anomaly",
        "date": latest["date"],
        "status": "open",
        "actual": money(actual, estimated),
        "expected": money(expected),
        "baseline_days": len(baseline),
        "threshold_percent": percent,
        "explanation": explanation,
        "top_services": top,
        "source": latest["source"],
        "billing_fetched_at": latest["fetched_at"],
        "created_at": iso(now),
        "expires_at": int((now + timedelta(days=ALERT_TTL_DAYS)).timestamp()),
    }
    return result
