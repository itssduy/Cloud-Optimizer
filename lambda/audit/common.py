"""Small helpers the newer checks share."""
from datetime import timedelta

MIN_COVERAGE = 0.8


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def window_text(days):
    minutes = round(days * 1440)
    if minutes < 60:
        return f"{minutes} minutes"
    if days < 1:
        hours = round(minutes / 60)
        return f"{hours} hour" + ("" if hours == 1 else "s")
    return f"{days:g} days"


def metric(cloudwatch, namespace, name, dimensions, statistic, now, days):
    """Datapoints for one metric over the window, plus how many we expected.

    Hourly points normally, 5-minute points for short demo windows.
    """
    start = now - timedelta(days=days)
    period = 3600 if days >= 1 else 300
    points = cloudwatch.get_metric_statistics(
        Namespace=namespace,
        MetricName=name,
        Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
        StartTime=start,
        EndTime=now,
        Period=period,
        Statistics=[statistic],
    )["Datapoints"]
    return {
        "metric": name,
        "statistic": statistic,
        "values": [p[statistic] for p in points],
        "datapoints": len(points),
        "expected_datapoints": round(days * 86400 / period),
        "period_days": days,
        "window": window_text(days),
        "window_start": iso(start),
        "window_end": iso(now),
    }


def resource(account_id, region, resource_type, resource_id, name, state, details):
    return {
        "account_id": account_id,
        "resource_type": resource_type,
        "region": region,
        "resource_id": resource_id,
        "name": name,
        "state": state,
        "details": details,
    }


def finding(r, rule, reason, evidence, recommendation, scan_id, now):
    return {
        "pk": f"ACCT#{r['account_id']}",
        "sk": f"FIND#{r['resource_type']}#{r['region']}#{r['resource_id']}#{rule}",
        "type": "finding",
        "account_id": r["account_id"],
        "resource_type": r["resource_type"],
        "region": r["region"],
        "resource_id": r["resource_id"],
        "rule": rule,
        "status": "open",
        "reason": reason,
        "evidence": evidence,
        "recommendation": recommendation,
        "estimated_monthly_savings": None,
        "scan_id": scan_id,
        "first_detected_at": iso(now),
        "last_detected_at": iso(now),
        "resolved_at": None,
    }


def result(r, outcome=None, note=None):
    return {"resource": r, "outcome": outcome, "note": note, "findings": []}


def tags_of(items):
    return {t["Key"]: t["Value"] for t in items or []}
