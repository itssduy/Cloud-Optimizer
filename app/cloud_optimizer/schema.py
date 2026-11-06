"""DynamoDB single-table schema.

Everything lives in one table with these keys:

    pk, sk          main keys
    gsi1pk, gsi1sk  index "gsi1", only used to list every AWS connection for the audit
    expires_at      TTL in epoch seconds, DynamoDB deletes the item after this

Items by partition:

    USER#<user_id>                    PROFILE                              user
    EMAIL#<email>                     LOOKUP                               email -> user_id
    GITHUB#<github_id>                LOOKUP                               github id -> user_id
    USER#<user_id>                    CONN#<account_id>                    AWS connection

    ACCT#<user_id>#<account_id>       SCAN#<started_at>#<scan_id>          scan run (history, 90 days)
    ACCT#<user_id>#<account_id>       RES#<type>#<region>#<resource_id>    resource (current only)
    ACCT#<user_id>#<account_id>       FIND#<type>#<region>#<id>#<rule>     finding (current only)
    ACCT#<user_id>#<account_id>       COST#<yyyy-mm-dd>                    daily spend (history, 400 days)
    ACCT#<user_id>#<account_id>       SAVINGS                              potential savings (current only)
    ACCT#<user_id>#<account_id>       ALERT#<date>#<alert_id>              spend anomaly (history, 400 days)
    ACCT#<user_id>#<account_id>       ACTION#<requested_at>#<action_id>    start/stop/lifecycle action (kept)

All account data is under one partition per user + account, so a user can only
ever read their own data by building the pk from their own user_id. Two users
connecting the same AWS account get separate copies.

Repeat scans overwrite resources and findings by key, so there are no duplicates.
A finding keeps its first_detected_at, and gets status "resolved" when a completed
scan doesn't see it again. A partial or failed scan never resolves findings for
the services that failed.

Money is {"amount": Decimal, "currency": "USD", "estimated": bool}. None means we
don't know the value, which is different from an amount of 0.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

SCAN_TTL_DAYS = 90
COST_TTL_DAYS = 400
ALERT_TTL_DAYS = 400

SCAN_STATUSES = ("running", "completed", "partial", "failed")
CONNECTION_STATUSES = ("pending", "connected", "failed", "disconnected")
FINDING_STATUSES = ("open", "resolved")
RESOURCE_TYPES = ("ec2_instance", "s3_bucket")
ACTION_TYPES = ("ec2_stop", "ec2_start", "s3_apply_lifecycle")
ACTION_REQUEST_STATUSES = ("pending", "sent", "failed")
ACTION_OUTCOMES = ("unknown", "verified", "failed")


# helpers

def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ttl(dt, days):
    return int((dt + timedelta(days=days)).timestamp())


def new_id():
    return uuid.uuid4().hex[:12]


def money(amount, currency="USD", estimated=False):
    if amount is None:
        return None
    return {
        "amount": Decimal(str(amount)).quantize(Decimal("0.01")),
        "currency": currency,
        "estimated": estimated,
    }


def for_dynamo(value):
    """boto3 doesn't accept floats, so convert them to Decimal before put_item."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: for_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [for_dynamo(v) for v in value]
    return value


def _check(value, allowed, name):
    if value not in allowed:
        raise ValueError(f"{name} must be one of {allowed}, got {value!r}")


# keys

def user_pk(user_id):
    return f"USER#{user_id}"


def account_pk(user_id, account_id):
    return f"ACCT#{user_id}#{account_id}"


def resource_sk(resource_type, region, resource_id):
    return f"RES#{resource_type}#{region}#{resource_id}"


def finding_sk(resource_type, region, resource_id, rule):
    return f"FIND#{resource_type}#{region}#{resource_id}#{rule}"


def resource_ref(resource_type, region, resource_id):
    return {"resource_type": resource_type, "region": region, "resource_id": resource_id}


# users

def user_item(user_id, email, name, password_hash=None, github_id=None, created_at=None):
    created_at = created_at or now()
    return {
        "pk": user_pk(user_id),
        "sk": "PROFILE",
        "type": "user",
        "user_id": user_id,
        "email": email.lower(),
        "name": name,
        "password_hash": password_hash,
        "github_id": github_id,
        "created_at": iso(created_at),
    }


def email_lookup_item(email, user_id):
    # put with ConditionExpression="attribute_not_exists(pk)" so emails stay unique
    return {"pk": f"EMAIL#{email.lower()}", "sk": "LOOKUP", "type": "email_lookup", "user_id": user_id}


def github_lookup_item(github_id, user_id):
    return {"pk": f"GITHUB#{github_id}", "sk": "LOOKUP", "type": "github_lookup", "user_id": user_id}


# AWS connections

def connection_item(user_id, account_id, role_arn, external_id, status="pending",
                    regions=("us-east-1",), verified_at=None, last_scan_at=None,
                    error=None, created_at=None, updated_at=None):
    _check(status, CONNECTION_STATUSES, "status")
    created_at = created_at or now()
    return {
        "pk": user_pk(user_id),
        "sk": f"CONN#{account_id}",
        "gsi1pk": "CONNECTIONS",
        "gsi1sk": f"{user_id}#{account_id}",
        "type": "connection",
        "user_id": user_id,
        "account_id": account_id,
        "role_arn": role_arn,
        "external_id": external_id,
        "regions": list(regions),
        "status": status,
        "error": error,
        "verified_at": iso(verified_at) if verified_at else None,
        "last_scan_at": iso(last_scan_at) if last_scan_at else None,
        "created_at": iso(created_at),
        "updated_at": iso(updated_at or created_at),
    }


# scans

def scan_item(user_id, account_id, scan_id, started_at, status="running", regions=("us-east-1",),
              completed_at=None, resource_count=0, finding_count=0, errors=()):
    """errors: [{"service": "s3", "region": "us-east-1", "message": "..."}]"""
    _check(status, SCAN_STATUSES, "status")
    return {
        "pk": account_pk(user_id, account_id),
        "sk": f"SCAN#{iso(started_at)}#{scan_id}",
        "type": "scan",
        "user_id": user_id,
        "account_id": account_id,
        "scan_id": scan_id,
        "status": status,
        "regions": list(regions),
        "started_at": iso(started_at),
        "completed_at": iso(completed_at) if completed_at else None,
        "resource_count": resource_count,
        "finding_count": finding_count,
        "errors": list(errors),
        "expires_at": ttl(started_at, SCAN_TTL_DAYS),
    }


# resources

def resource_item(user_id, account_id, resource_type, region, resource_id, name, state,
                  details, scan_id, first_seen_at, last_seen_at):
    """details is per type, e.g. ec2: instance_type, launch_time; s3: object_count, size_bytes."""
    _check(resource_type, RESOURCE_TYPES, "resource_type")
    return {
        "pk": account_pk(user_id, account_id),
        "sk": resource_sk(resource_type, region, resource_id),
        "type": "resource",
        "user_id": user_id,
        "account_id": account_id,
        **resource_ref(resource_type, region, resource_id),
        "name": name,
        "state": state,
        "details": details,
        "last_scan_id": scan_id,
        "first_seen_at": iso(first_seen_at),
        "last_seen_at": iso(last_seen_at),
    }


# findings

def finding_item(user_id, account_id, resource_type, region, resource_id, rule, reason,
                 evidence, recommendation, estimated_monthly_savings, scan_id,
                 first_detected_at, last_detected_at, status="open", resolved_at=None):
    """
    evidence: what the rule looked at, including coverage, e.g.
        {"metric": "CPUUtilization", "average": 1.2, "threshold": 5, "period_days": 7,
         "datapoints": 168, "expected_datapoints": 168}
    recommendation: {"action": "ec2_stop" | None, "text": "..."}
    estimated_monthly_savings: money(...) or None if we can't estimate it
    """
    _check(resource_type, RESOURCE_TYPES, "resource_type")
    _check(status, FINDING_STATUSES, "status")
    return {
        "pk": account_pk(user_id, account_id),
        "sk": finding_sk(resource_type, region, resource_id, rule),
        "type": "finding",
        "user_id": user_id,
        "account_id": account_id,
        **resource_ref(resource_type, region, resource_id),
        "rule": rule,
        "status": status,
        "reason": reason,
        "evidence": evidence,
        "recommendation": recommendation,
        "estimated_monthly_savings": estimated_monthly_savings,
        "scan_id": scan_id,
        "first_detected_at": iso(first_detected_at),
        "last_detected_at": iso(last_detected_at),
        "resolved_at": iso(resolved_at) if resolved_at else None,
    }


# spending and savings

def daily_cost_item(user_id, account_id, date, total, by_service, fetched_at, source="cost_explorer"):
    """date is "YYYY-MM-DD". No item for a day means we don't have data, not $0."""
    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return {
        "pk": account_pk(user_id, account_id),
        "sk": f"COST#{date}",
        "type": "daily_cost",
        "user_id": user_id,
        "account_id": account_id,
        "date": date,
        "total": total,
        "by_service": by_service,
        "source": source,
        "fetched_at": iso(fetched_at),
        "expires_at": ttl(day, COST_TTL_DAYS),
    }


def savings_item(user_id, account_id, total_monthly, finding_count, missing_estimate_count,
                 assumptions, calculated_at):
    """missing_estimate_count: findings we couldn't put a dollar amount on (not counted in total)."""
    return {
        "pk": account_pk(user_id, account_id),
        "sk": "SAVINGS",
        "type": "savings",
        "user_id": user_id,
        "account_id": account_id,
        "total_monthly": total_monthly,
        "finding_count": finding_count,
        "missing_estimate_count": missing_estimate_count,
        "assumptions": list(assumptions),
        "calculated_at": iso(calculated_at),
    }


# alerts

def alert_item(user_id, account_id, alert_id, date, actual, expected, baseline_days,
               explanation, top_services, created_at, status="open"):
    day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return {
        "pk": account_pk(user_id, account_id),
        "sk": f"ALERT#{date}#{alert_id}",
        "type": "alert",
        "user_id": user_id,
        "account_id": account_id,
        "alert_id": alert_id,
        "alert_type": "spend_anomaly",
        "date": date,
        "status": status,
        "actual": actual,
        "expected": expected,
        "baseline_days": baseline_days,
        "explanation": explanation,
        "top_services": top_services,
        "created_at": iso(created_at),
        "expires_at": ttl(day, ALERT_TTL_DAYS),
    }


# actions

def action_item(user_id, account_id, action_id, action_type, resource_type, region, resource_id,
                change, requested_by, requested_at, request_status="pending", request_error=None,
                outcome="unknown", outcome_checked_at=None):
    """change is what the user reviewed before confirming: {"before": {...}, "after": {...}}"""
    _check(action_type, ACTION_TYPES, "action_type")
    _check(request_status, ACTION_REQUEST_STATUSES, "request_status")
    _check(outcome, ACTION_OUTCOMES, "outcome")
    return {
        "pk": account_pk(user_id, account_id),
        "sk": f"ACTION#{iso(requested_at)}#{action_id}",
        "type": "action",
        "user_id": user_id,
        "account_id": account_id,
        "action_id": action_id,
        "action_type": action_type,
        "target": resource_ref(resource_type, region, resource_id),
        "change": change,
        "requested_by": requested_by,
        "requested_at": iso(requested_at),
        "request_status": request_status,
        "request_error": request_error,
        "outcome": outcome,
        "outcome_checked_at": iso(outcome_checked_at) if outcome_checked_at else None,
    }
