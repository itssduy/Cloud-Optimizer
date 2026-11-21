"""People who have signed in. Cognito decides who can sign in; this just remembers them
so actions can show who did what."""

import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

from . import schema


def table():
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        endpoint_url=os.environ.get("DYNAMODB_ENDPOINT_URL") or None,
    )
    return dynamodb.Table(os.environ["DYNAMODB_TABLE_NAME"])


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def signed_in(sub, email):
    """Creates or updates the user record after a Cognito login. user_id is the Cognito sub."""
    return table().update_item(
        Key={"pk": schema.USERS_PK, "sk": schema.user_sk(sub)},
        UpdateExpression=("SET #t = :t, user_id = :id, email = :e, last_login_at = :now, "
                          "#s = if_not_exists(#s, :active), created_at = if_not_exists(created_at, :now)"),
        ExpressionAttributeNames={"#t": "type", "#s": "status"},
        ExpressionAttributeValues={":t": "user", ":id": sub, ":e": email, ":now": now(), ":active": "active"},
        ReturnValues="ALL_NEW",
    )["Attributes"]


def get_user(user_id):
    return table().get_item(Key={"pk": schema.USERS_PK, "sk": schema.user_sk(user_id)}).get("Item")


def list_users():
    items = table().query(KeyConditionExpression=Key("pk").eq(schema.USERS_PK))["Items"]
    return sorted(items, key=lambda u: u["email"])


def update_user(user_id, **fields):
    table().update_item(
        Key={"pk": schema.USERS_PK, "sk": schema.user_sk(user_id)},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames={f"#{k}": k for k in fields},
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
        ConditionExpression="attribute_exists(pk)",
    )
