import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from django.contrib.auth.hashers import check_password, make_password

from . import schema

ROLES = ("admin", "member")


def table():
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        endpoint_url=os.environ.get("DYNAMODB_ENDPOINT_URL") or None,
    )
    return dynamodb.Table(os.environ["DYNAMODB_TABLE_NAME"])


class EmailTaken(Exception):
    pass


def create_user(email, name, password, role="member"):
    if role not in ROLES:
        raise ValueError(f"bad role {role}")
    email = email.strip().lower()
    user = {
        "pk": schema.USERS_PK,
        "sk": schema.user_sk("u_" + uuid.uuid4().hex[:8]),
        "type": "user",
        "email": email,
        "name": name,
        "password_hash": make_password(password),
        "role": role,
        "status": "active",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    user["user_id"] = user["sk"].removeprefix("USER#")

    t = table()
    client = t.meta.client
    try:
        client.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": t.name,
                "Item": {"pk": schema.email_pk(email), "sk": "LOOKUP", "type": "email_lookup",
                         "user_id": user["user_id"]},
                "ConditionExpression": "attribute_not_exists(pk)",
            }},
            {"Put": {"TableName": t.name, "Item": user}},
        ])
    except client.exceptions.TransactionCanceledException:
        raise EmailTaken(email)
    return user


def get_user(user_id):
    return table().get_item(Key={"pk": schema.USERS_PK, "sk": schema.user_sk(user_id)}).get("Item")


def list_users():
    items = table().query(KeyConditionExpression=Key("pk").eq(schema.USERS_PK))["Items"]
    return sorted(items, key=lambda u: u["email"])


def has_users():
    return bool(table().query(KeyConditionExpression=Key("pk").eq(schema.USERS_PK), Limit=1)["Items"])


def authenticate(email, password):
    lookup = table().get_item(Key={"pk": schema.email_pk(email.strip()), "sk": "LOOKUP"}).get("Item")
    user = get_user(lookup["user_id"]) if lookup else None
    if not user or not check_password(password, user["password_hash"]):
        return None
    if user["status"] != "active":
        return None
    return user


def update_user(user_id, **fields):
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    table().update_item(
        Key={"pk": schema.USERS_PK, "sk": schema.user_sk(user_id)},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ConditionExpression="attribute_exists(pk)",
    )


def set_password(user_id, password):
    update_user(user_id, password_hash=make_password(password))
