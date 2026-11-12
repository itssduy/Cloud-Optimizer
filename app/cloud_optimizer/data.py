import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from . import schema
from .users import table

RESOURCE_TYPES = ("ec2_instance", "s3_bucket")
RESOURCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
HIDDEN = ("pk", "sk", "expires_at")


class NotFound(Exception):
    pass


def account_id():
    return os.environ["AWS_ACCOUNT_ID"]


def region():
    return os.environ.get("AWS_REGION", "us-east-1")


def clean(value):
    """DynamoDB item -> plain JSON-friendly dict."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k not in HIDDEN}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def _query(prefix, newest_first=False, limit=None, start=None):
    if start:
        key = Key("pk").eq(schema.account_pk(account_id())) & Key("sk").between(start, prefix + "￿")
    else:
        key = Key("pk").eq(schema.account_pk(account_id())) & Key("sk").begins_with(prefix)
    kwargs = {"KeyConditionExpression": key, "ScanIndexForward": not newest_first}
    if limit:
        kwargs["Limit"] = limit
    items = []
    while True:
        page = table().query(**kwargs)
        items.extend(page["Items"])
        if (limit and len(items) >= limit) or "LastEvaluatedKey" not in page:
            return [clean(i) for i in items[:limit] if i.get("region", region()) == region()]
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _check_resource(resource_type, resource_id):
    if resource_type not in RESOURCE_TYPES or not RESOURCE_ID.match(resource_id):
        raise NotFound


def status():
    """For the deployment status page (#38)."""
    deployment = table().get_item(Key={"pk": schema.DEPLOYMENT_PK, "sk": "CONFIG"}).get("Item")
    latest = scans(limit=1)
    return {
        "account_id": account_id(),
        "region": region(),
        "deployment": clean(deployment) if deployment else None,
        "latest_scan": latest[0] if latest else None,
        "scan_state": latest[0]["status"] if latest else "no_scan",
    }


def scans(limit=20):
    return _query("SCAN#", newest_first=True, limit=limit)


def resources(resource_type=None):
    if resource_type:
        if resource_type not in RESOURCE_TYPES:
            raise NotFound
        return _query(f"RES#{resource_type}#{region()}#")
    return _query("RES#")


def resource(resource_type, resource_id):
    _check_resource(resource_type, resource_id)
    item = table().get_item(Key={
        "pk": schema.account_pk(account_id()),
        "sk": schema.resource_sk(resource_type, region(), resource_id),
    }).get("Item")
    if not item:
        raise NotFound
    return {
        "resource": clean(item),
        "findings": _query(f"FIND#{resource_type}#{region()}#{resource_id}#"),
        "actions": [a for a in actions() if a["target"]["resource_id"] == resource_id],
    }


def findings(status="open"):
    items = _query("FIND#")
    return [f for f in items if status is None or f["status"] == status]


def costs(days=30):
    start = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    return _query("COST#", start=schema.cost_sk(start))


def savings():
    item = table().get_item(Key={"pk": schema.account_pk(account_id()), "sk": "SAVINGS"}).get("Item")
    return clean(item) if item else None


def alerts(status=None):
    items = _query("ALERT#", newest_first=True)
    return [a for a in items if status is None or a["status"] == status]


def actions(limit=50):
    return _query("ACTION#", newest_first=True, limit=limit)
