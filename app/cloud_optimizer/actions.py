import re
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from . import auth, data, schema
from .users import table

INSTANCE_ID = re.compile(r"^i-[0-9a-f]{8,17}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
ACTIONS = {"stop": ("running", "stopped"), "start": ("stopped", "running")}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ec2():
    return boto3.client("ec2", region_name=data.region())


def error(code, status, **extra):
    return JsonResponse({"error": code, **extra}, status=status)


def describe(instance_id):
    """Current instance from AWS, or None if it isn't in this account/region."""
    try:
        reservations = ec2().describe_instances(InstanceIds=[instance_id])["Reservations"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
            return None
        raise
    for r in reservations:
        if r.get("OwnerId") == data.account_id():
            for i in r["Instances"]:
                return i
    return None


def blocked_reason(instance):
    if instance.get("InstanceLifecycle") == "spot":
        return "spot instance"
    if any(t["Key"] == "aws:autoscaling:groupName" for t in instance.get("Tags", [])):
        return "managed by an auto scaling group"
    return None


@require_POST
def request_action(request, instance_id, action):
    """Start or stop one instance. Needs confirm=yes and a unique request_id from the page."""
    user = auth.current_user(request)
    if not user:
        return error("not_logged_in", 401)
    if action not in ACTIONS or not INSTANCE_ID.match(instance_id):
        return error("not_found", 404)
    if request.POST.get("confirm") != "yes":
        return error("not_confirmed", 400)
    request_id = request.POST.get("request_id", "")
    if not REQUEST_ID.match(request_id):
        return error("bad_request_id", 400)

    needed_state, target_state = ACTIONS[action]
    t = table()
    pk = schema.account_pk(data.account_id())
    requested_at = now()
    action_sk = schema.action_sk(requested_at, request_id)

    try:
        # claim the request id first, so a repeated or replayed request does nothing
        t.put_item(
            Item={"pk": pk, "sk": f"ACTIONREQ#{request_id}", "type": "action_request", "action_sk": action_sk},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except t.meta.client.exceptions.ConditionalCheckFailedException:
        return error("duplicate_request", 409)

    try:
        instance = describe(instance_id)
    except (BotoCoreError, ClientError):
        return error("aws_unavailable", 503)
    if not instance:
        return error("not_found", 404)
    reason = blocked_reason(instance)
    if reason:
        return error("not_allowed", 409, reason=reason)
    state = instance["State"]["Name"]
    if state != needed_state:
        return error("wrong_state", 409, state=state, needed=needed_state)

    item = {
        "pk": pk,
        "sk": action_sk,
        "type": "action",
        "account_id": data.account_id(),
        "action_id": request_id,
        "action_type": f"ec2_{action}",
        "target": {"resource_type": "ec2_instance", "region": data.region(), "resource_id": instance_id},
        "change": {"before": {"state": state}, "after": {"state": target_state}},
        "requested_by": user["user_id"],
        "requested_at": requested_at,
        "request_status": "sent",
        "request_error": None,
        "observed_state": None,
        "outcome": "unknown",
        "outcome_checked_at": None,
    }
    try:
        call = ec2().stop_instances if action == "stop" else ec2().start_instances
        changed = call(InstanceIds=[instance_id])
        key = "StoppingInstances" if action == "stop" else "StartingInstances"
        item["observed_state"] = changed[key][0]["CurrentState"]["Name"]
    except (BotoCoreError, ClientError) as e:
        item["request_status"] = "failed"
        item["request_error"] = getattr(e, "response", {}).get("Error", {}).get("Code", str(e))
        item["outcome"] = "failed"
    t.put_item(Item=item)
    return JsonResponse({"action": data.clean(item)}, status=202 if item["request_status"] == "sent" else 502)


@require_GET
def action_status(request, request_id):
    """Checks the instance again and marks the action verified once it reaches the target state."""
    if not auth.current_user(request):
        return error("not_logged_in", 401)
    if not REQUEST_ID.match(request_id):
        return error("not_found", 404)
    t = table()
    pk = schema.account_pk(data.account_id())
    ref = t.get_item(Key={"pk": pk, "sk": f"ACTIONREQ#{request_id}"}).get("Item")
    item = t.get_item(Key={"pk": pk, "sk": ref["action_sk"]}).get("Item") if ref else None
    if not item:
        return error("not_found", 404)

    if item["request_status"] == "sent" and item["outcome"] == "unknown":
        try:
            instance = describe(item["target"]["resource_id"])
        except (BotoCoreError, ClientError):
            return JsonResponse({"action": data.clean(item)})
        state = instance["State"]["Name"] if instance else "missing"
        item["observed_state"] = state
        item["outcome_checked_at"] = now()
        if state == item["change"]["after"]["state"]:
            item["outcome"] = "verified"
        t.put_item(Item=item)
    return JsonResponse({"action": data.clean(item)})
