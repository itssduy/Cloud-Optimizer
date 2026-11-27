import hashlib
import json
import re
from time import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.core.serializers.json import DjangoJSONEncoder
from django.http import JsonResponse

from . import auth, data, schema
from .actions import REQUEST_ID, get_action, now
from .users import table


def error(code, status, **extra):
    return {"error": code, **extra}, status


BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
RULES = {
    "s3_incomplete_uploads": {
        "effect": "S3 will abort eligible uploads that are still incomplete at the configured age.",
        "charges": "Uploaded parts can accrue storage charges until the upload is completed or aborted.",
    },
    "s3_tiering_candidate": {
        "effect": "S3 will schedule eligible objects to move into Intelligent-Tiering. Future savings depend on access patterns.",
        "charges": "Transition requests and ongoing per-object monitoring can incur charges.",
    },
}


def s3():
    return boto3.client("s3", region_name=data.region())


def target(bucket):
    return {"resource_type": "s3_bucket", "region": data.region(), "resource_id": bucket}


def current_configuration(bucket):
    """Read the configuration after checking bucket owner and region."""
    client = s3()
    owner = {"ExpectedBucketOwner": data.account_id()}
    location = client.get_bucket_location(Bucket=bucket, **owner)["LocationConstraint"] or "us-east-1"
    if location == "EU":
        location = "eu-west-1"
    if location != data.region():
        return None
    try:
        response = client.get_bucket_lifecycle_configuration(Bucket=bucket, **owner)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchLifecycleConfiguration":
            return {"Rules": []}
        raise
    configuration = {"Rules": response["Rules"]}
    if "TransitionDefaultMinimumObjectSize" in response:
        configuration["TransitionDefaultMinimumObjectSize"] = response["TransitionDefaultMinimumObjectSize"]
    return configuration


def proposal_hash(configuration, proposed):
    blob = json.dumps({"current": configuration, "proposed": proposed}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def safe_rule(rule):
    """Only the two rule shapes we support. No expiry, no archive tiers."""
    allowed = {"ID", "Status", "Filter", "AbortIncompleteMultipartUpload", "Transitions"}
    if set(rule) - allowed or "Expiration" in rule or "NoncurrentVersionExpiration" in rule:
        return False
    return all(t.get("StorageClass") == "INTELLIGENT_TIERING" for t in rule.get("Transitions", []))


def load_proposal(bucket, rule_name):
    """Load the saved finding and the live configuration used for review."""
    if not BUCKET.fullmatch(bucket) or rule_name not in RULES:
        return error("not_found", 404)
    finding = table().get_item(Key={
        "pk": schema.account_pk(data.account_id()),
        "sk": schema.finding_sk("s3_bucket", data.region(), bucket, rule_name),
    }, ConsistentRead=True).get("Item")
    if not finding or finding["status"] != "open":
        return error("no_open_finding", 409)
    proposed = (finding.get("recommendation") or {}).get("proposed_rule")
    if not proposed or not safe_rule(proposed):
        return error("unsupported_rule", 409)
    proposed = data.clean(proposed)
    try:
        configuration = current_configuration(bucket)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("AccessDenied", "NoSuchBucket", "403"):
            return error("not_found", 404)
        return error("aws_unavailable", 503, detail=code)
    except BotoCoreError:
        return error("aws_unavailable", 503)
    if configuration is None:
        return error("not_found", 404)
    return {"finding": finding, "proposed": proposed, "configuration": configuration}, 200


def lifecycle(request, bucket):
    user = auth.current_user(request)
    if not user:
        result, status = error("not_logged_in", 401)
    elif request.method == "GET":
        result, status = preview(bucket, request.GET.get("rule", ""))
    elif request.method == "POST":
        result, status = apply(
            bucket, user, request.POST.get("rule", ""), request.POST.get("confirm"),
            request.POST.get("request_id", ""), request.POST.get("proposal_hash"),
        )
    else:
        result, status = error("method_not_allowed", 405)
    return JsonResponse(result, status=status)


def preview(bucket, rule_name):
    loaded, status = load_proposal(bucket, rule_name)
    if status != 200:
        return loaded, status
    proposed = loaded["proposed"]
    configuration = loaded["configuration"]
    rules = configuration["Rules"]
    already = any(r.get("ID") == proposed["ID"] for r in rules)
    return {
        "bucket": bucket,
        "account_id": data.account_id(),
        "region": data.region(),
        "rule": rule_name,
        "reason": loaded["finding"]["reason"],
        "current_rules": rules,
        "proposed_rule": proposed,
        "result_rules": rules if already else rules + [proposed],
        "already_applied": already,
        "effect": RULES[rule_name]["effect"],
        "charges": RULES[rule_name]["charges"],
        "proposal_hash": proposal_hash(configuration, proposed),
    }, 200


def apply(bucket, user, rule_name, confirm, request_id, reviewed_hash):
    if confirm != "yes":
        return error("not_confirmed", 400)
    if not REQUEST_ID.fullmatch(request_id):
        return error("bad_request_id", 400)
    if not BUCKET.fullmatch(bucket) or rule_name not in RULES:
        return error("not_found", 404)

    # S3 replaces the whole configuration. Serialize writes from this application to one bucket.
    t = table()
    pk = schema.account_pk(data.account_id())
    lock = {"pk": pk, "sk": f"S3LIFECYCLE#{data.region()}#{bucket}"}
    current_time = int(time())
    expires_at = current_time + 300
    try:
        t.put_item(
            Item={**lock, "expires_at": expires_at},
            ConditionExpression="attribute_not_exists(pk) OR expires_at < :now",
            ExpressionAttributeValues={":now": current_time},
        )
    except t.meta.client.exceptions.ConditionalCheckFailedException:
        return error("bucket_busy", 409)
    try:
        return apply_proposal(t, pk, bucket, user, rule_name, request_id, reviewed_hash)
    finally:
        try:
            t.delete_item(Key=lock, ConditionExpression="expires_at = :expiry",
                          ExpressionAttributeValues={":expiry": expires_at})
        except t.meta.client.exceptions.ConditionalCheckFailedException:
            pass


def apply_proposal(t, pk, bucket, user, rule_name, request_id, reviewed_hash):
    loaded, status = load_proposal(bucket, rule_name)
    if status != 200:
        return loaded, status
    configuration = loaded["configuration"]
    proposed = loaded["proposed"]
    rules = configuration["Rules"]
    if reviewed_hash != proposal_hash(configuration, proposed):
        return error("stale_proposal", 409)
    if any(r.get("ID") == proposed["ID"] for r in rules):
        return error("already_applied", 409)

    recorded_rules = json.loads(json.dumps(rules, cls=DjangoJSONEncoder))
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
        "target": target(bucket),
        "rule": rule_name,
        "change": {"before": {"rules": recorded_rules}, "after": {"rules": recorded_rules + [proposed]}},
        "requested_by": user["user_id"],
        "requested_at": requested_at,
        "request_status": "pending",
        "request_error": None,
        "outcome": "unknown",
        "outcome_checked_at": None,
        "effect": RULES[rule_name]["effect"],
    }
    t.put_item(Item=item)
    options = {}
    if "TransitionDefaultMinimumObjectSize" in configuration:
        options["TransitionDefaultMinimumObjectSize"] = configuration["TransitionDefaultMinimumObjectSize"]
    try:
        # existing rules are sent back unchanged, only the new rule is added
        s3().put_bucket_lifecycle_configuration(
            Bucket=bucket, ExpectedBucketOwner=data.account_id(),
            LifecycleConfiguration={"Rules": rules + [proposed]}, **options,
        )
    except (BotoCoreError, ClientError) as e:
        item["request_status"] = "failed"
        item["request_error"] = getattr(e, "response", {}).get("Error", {}).get("Code", str(e))
        item["outcome"] = "failed"
    else:
        item["request_status"] = "sent"
        verify_configuration(item)
    t.put_item(Item=item)
    return {"action": data.clean(item)}, 202 if item["request_status"] == "sent" else 502


def comparable_rule(rule):
    # S3 can return an empty filter or the legacy empty prefix for a bucket-wide rule.
    if not rule.get("Prefix") and rule.get("Filter") in (None, {}, {"Prefix": ""}):
        return {key: value for key, value in rule.items() if key not in ("Prefix", "Filter")}
    return rule


def verify_configuration(item):
    try:
        saved = current_configuration(item["target"]["resource_id"])
    except (BotoCoreError, ClientError):
        return
    proposed = comparable_rule(item["change"]["after"]["rules"][-1])
    item["outcome_checked_at"] = now()
    if saved and any(comparable_rule(rule) == proposed for rule in saved["Rules"]):
        item["outcome"] = "verified"


def saved_action(bucket, request_id):
    item = get_action(request_id)
    if item and item["action_type"] == "s3_apply_lifecycle" and item["target"] == target(bucket):
        return item
    return None


def check_action(bucket, request_id):
    item = saved_action(bucket, request_id)
    if item and item["request_status"] == "sent" and item["outcome"] == "unknown":
        verify_configuration(item)
        if item["outcome"] == "verified":
            table().put_item(Item=item)
    return item
