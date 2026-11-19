import hashlib
import json
import re

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.http import JsonResponse

from . import auth, data, schema
from .actions import REQUEST_ID, now
from .users import table

def error(code, status, **extra):
    return JsonResponse({"error": code, **extra}, status=status)


BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
RULES = {
    "s3_incomplete_uploads": {
        "effect": "S3 will start aborting incomplete uploads older than the set age. Cleanup happens over the next day or two.",
        "charges": "No extra charge.",
    },
    "s3_tiering_candidate": {
        "effect": "S3 will start moving objects into Intelligent-Tiering. Savings only show up if objects aren't read for 30+ days.",
        "charges": "Intelligent-Tiering adds a small monitoring fee per object and a one-time transition request charge.",
    },
}


def s3():
    return boto3.client("s3", region_name=data.region())


def current_rules(bucket):
    """Lifecycle rules on the bucket, checking it belongs to this account and region."""
    client = s3()
    owner = {"ExpectedBucketOwner": data.account_id()}
    location = client.get_bucket_location(Bucket=bucket, **owner)["LocationConstraint"] or "us-east-1"
    if location != data.region():
        return None
    try:
        return client.get_bucket_lifecycle_configuration(Bucket=bucket, **owner)["Rules"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
            return []
        raise


def proposal_hash(rules, proposed):
    blob = json.dumps({"current": rules, "proposed": proposed}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def safe_rule(rule):
    """Only the two rule shapes we support. No expiry, no archive tiers."""
    allowed = {"ID", "Status", "Filter", "AbortIncompleteMultipartUpload", "Transitions"}
    if set(rule) - allowed or "Expiration" in rule or "NoncurrentVersionExpiration" in rule:
        return False
    return all(t.get("StorageClass") == "INTELLIGENT_TIERING" for t in rule.get("Transitions", []))


def load_proposal(bucket, rule_name):
    """Returns (finding, proposed rule, current rules) or a JsonResponse error."""
    if not BUCKET.match(bucket) or rule_name not in RULES:
        return error("not_found", 404)
    finding = table().get_item(Key={
        "pk": schema.account_pk(data.account_id()),
        "sk": schema.finding_sk("s3_bucket", data.region(), bucket, rule_name),
    }).get("Item")
    if not finding or finding["status"] != "open":
        return error("no_open_finding", 409)
    proposed = finding["recommendation"].get("proposed_rule")
    if not proposed or not safe_rule(proposed):
        return error("unsupported_rule", 409)
    proposed = data.clean(proposed)
    try:
        rules = current_rules(bucket)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("AccessDenied", "NoSuchBucket", "403"):
            return error("not_found", 404)
        return error("aws_unavailable", 503, detail=code)
    except BotoCoreError:
        return error("aws_unavailable", 503)
    if rules is None:
        return error("not_found", 404)
    return finding, proposed, rules


def lifecycle(request, bucket):
    user = auth.current_user(request)
    if not user:
        return error("not_logged_in", 401)
    if request.method == "GET":
        return preview(request, bucket)
    if request.method == "POST":
        return apply(request, bucket, user)
    return error("method_not_allowed", 405)


def preview(request, bucket):
    rule_name = request.GET.get("rule", "")
    loaded = load_proposal(bucket, rule_name)
    if isinstance(loaded, JsonResponse):
        return loaded
    finding, proposed, rules = loaded
    already = any(r.get("ID") == proposed["ID"] for r in rules)
    return JsonResponse({
        "bucket": bucket,
        "account_id": data.account_id(),
        "region": data.region(),
        "rule": rule_name,
        "reason": finding["reason"],
        "current_rules": rules,
        "proposed_rule": proposed,
        "result_rules": rules if already else rules + [proposed],
        "already_applied": already,
        "effect": RULES[rule_name]["effect"],
        "charges": RULES[rule_name]["charges"],
        "proposal_hash": proposal_hash(rules, proposed),
    })


def apply(request, bucket, user):
    rule_name = request.POST.get("rule", "")
    if request.POST.get("confirm") != "yes":
        return error("not_confirmed", 400)
    request_id = request.POST.get("request_id", "")
    if not REQUEST_ID.match(request_id):
        return error("bad_request_id", 400)

    loaded = load_proposal(bucket, rule_name)
    if isinstance(loaded, JsonResponse):
        return loaded
    finding, proposed, rules = loaded
    if request.POST.get("proposal_hash") != proposal_hash(rules, proposed):
        return error("stale_proposal", 409)
    if any(r.get("ID") == proposed["ID"] for r in rules):
        return error("already_applied", 409)

    t = table()
    pk = schema.account_pk(data.account_id())
    requested_at = now()
    action_sk = schema.action_sk(requested_at, request_id)
    try:
        t.put_item(
            Item={"pk": pk, "sk": f"ACTIONREQ#{request_id}", "type": "action_request", "action_sk": action_sk},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except t.meta.client.exceptions.ConditionalCheckFailedException:
        return error("duplicate_request", 409)

    item = {
        "pk": pk,
        "sk": action_sk,
        "type": "action",
        "account_id": data.account_id(),
        "action_id": request_id,
        "action_type": "s3_apply_lifecycle",
        "target": {"resource_type": "s3_bucket", "region": data.region(), "resource_id": bucket},
        "rule": rule_name,
        "change": {"before": {"rules": rules}, "after": {"rules": rules + [proposed]}},
        "requested_by": user["user_id"],
        "requested_at": requested_at,
        "request_status": "sent",
        "request_error": None,
        "outcome": "unknown",
        "outcome_checked_at": None,
        "effect": RULES[rule_name]["effect"],
    }
    try:
        # existing rules are sent back unchanged, only the new rule is added
        s3().put_bucket_lifecycle_configuration(
            Bucket=bucket, ExpectedBucketOwner=data.account_id(),
            LifecycleConfiguration={"Rules": rules + [proposed]},
        )
        saved = current_rules(bucket) or []
        item["outcome"] = "verified" if any(r.get("ID") == proposed["ID"] for r in saved) else "unknown"
        item["outcome_checked_at"] = now()
    except (BotoCoreError, ClientError) as e:
        item["request_status"] = "failed"
        item["request_error"] = getattr(e, "response", {}).get("Error", {}).get("Code", str(e))
        item["outcome"] = "failed"
    t.put_item(Item=item)
    return JsonResponse({"action": data.clean(item)}, status=202 if item["request_status"] == "sent" else 502)
