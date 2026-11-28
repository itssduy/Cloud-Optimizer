import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

import anomalies
import costs
import ebs
import ec2
import elb
import functions
import rds
import s3

LOCK_MINUTES = 15
SCAN_TTL_DAYS = 90
RESOURCE_TYPES = ["ec2_instance", "s3_bucket", "ebs_volume", "rds_instance", "lambda_function", "load_balancer"]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def for_dynamo(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: for_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [for_dynamo(v) for v in value]
    return value


def error_text(e):
    if isinstance(e, ClientError):
        return f"{e.response['Error']['Code']}: {e.response['Error'].get('Message', '')}"
    return str(e)


class Audit:
    def __init__(self, table, account_id, region, now, clients):
        self.table = table
        self.account_id = account_id
        self.region = region
        self.now = now
        self.clients = clients
        self.pk = f"ACCT#{account_id}"
        self.scan_id = "scan_" + uuid.uuid4().hex[:8]
        self.scan_sk = f"SCAN#{iso(now)}#{self.scan_id}"
        self.errors = []
        self.notes = []
        self.spending_check = {"outcome": "pending"}
        self.counts = {"resources": 0, "findings": 0, "insufficient_data": 0}

    # locking, so two runs at the same time don't fight over the same items

    def lock(self):
        try:
            self.table.put_item(
                Item={"pk": self.pk, "sk": "SCANLOCK", "scan_id": self.scan_id,
                      "until": iso(self.now + timedelta(minutes=LOCK_MINUTES))},
                ConditionExpression="attribute_not_exists(pk) OR #u < :now",
                ExpressionAttributeNames={"#u": "until"},
                ExpressionAttributeValues={":now": iso(self.now)},
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def unlock(self):
        self.table.delete_item(Key={"pk": self.pk, "sk": "SCANLOCK"},
                               ConditionExpression="scan_id = :id",
                               ExpressionAttributeValues={":id": self.scan_id})

    # saving

    def save_scan(self, status, completed=False):
        self.table.put_item(Item=for_dynamo({
            "pk": self.pk,
            "sk": self.scan_sk,
            "type": "scan",
            "account_id": self.account_id,
            "scan_id": self.scan_id,
            "status": status,
            "regions": [self.region],
            "started_at": iso(self.now),
            "completed_at": iso(datetime.now(timezone.utc)) if completed else None,
            "resource_count": self.counts["resources"],
            "finding_count": self.counts["findings"],
            "insufficient_data_count": self.counts["insufficient_data"],
            "errors": self.errors,
            "notes": self.notes,
            "spending_check": self.spending_check,
            "expires_at": int((self.now + timedelta(days=SCAN_TTL_DAYS)).timestamp()),
        }))

    def save_resource(self, result):
        r = result["resource"]
        self.table.update_item(
            Key={"pk": self.pk, "sk": f"RES#{r['resource_type']}#{r['region']}#{r['resource_id']}"},
            UpdateExpression=("SET #t = :t, account_id = :a, resource_type = :rt, #r = :r, resource_id = :id, "
                              "#n = :n, #s = :s, details = :d, last_check = :c, last_scan_id = :scan, "
                              "last_seen_at = :now, first_seen_at = if_not_exists(first_seen_at, :now)"),
            ExpressionAttributeNames={"#t": "type", "#r": "region", "#n": "name", "#s": "state"},
            ExpressionAttributeValues=for_dynamo({
                ":t": "resource", ":a": self.account_id, ":rt": r["resource_type"], ":r": r["region"],
                ":id": r["resource_id"], ":n": r["name"], ":s": r["state"], ":d": r["details"],
                ":c": {"outcome": result["outcome"], "note": result["note"]},
                ":scan": self.scan_id, ":now": iso(self.now),
            }),
        )

    def save_finding(self, f):
        fields = {k: v for k, v in f.items() if k not in ("pk", "sk", "first_detected_at")}
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":f{i}": v for i, v in enumerate(fields.values())}
        sets = [f"#f{i} = :f{i}" for i in range(len(fields))]
        sets.append("first_detected_at = if_not_exists(first_detected_at, :first)")
        values[":first"] = f["first_detected_at"]
        self.table.update_item(
            Key={"pk": f["pk"], "sk": f["sk"]},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=for_dynamo(values),
        )

    def query(self, prefix):
        items, kwargs = [], {"KeyConditionExpression": Key("pk").eq(self.pk) & Key("sk").begins_with(prefix)}
        while True:
            page = self.table.query(**kwargs)
            items.extend(page["Items"])
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def apply(self, resource_type, results):
        """Saves one source's results, then cleans up anything that source no longer sees.

        Resources and findings for a resource that errored are left alone, so a
        failed read never makes something look fixed or deleted.
        """
        seen_resources, seen_findings, errored = set(), set(), set()
        for result in results:
            r = result["resource"]
            if result["outcome"] == "error":
                errored.add(r["resource_id"])
                self.errors.append({"service": resource_type, "region": self.region,
                                    "resource_id": r["resource_id"], "message": result["note"]})
                continue
            self.save_resource(result)
            seen_resources.add(r["resource_id"])
            self.counts["resources"] += 1
            if result["outcome"] == "insufficient_data":
                self.counts["insufficient_data"] += 1
            for f in result["findings"]:
                self.save_finding(f)
                seen_findings.add(f["sk"])
                self.counts["findings"] += 1

        prefix = f"#{resource_type}#{self.region}#"
        for item in self.query("RES" + prefix):
            if item["resource_id"] not in seen_resources | errored:
                self.table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
        for item in self.query("FIND" + prefix):
            if item["status"] == "open" and item["sk"] not in seen_findings and item["resource_id"] not in errored:
                self.table.update_item(
                    Key={"pk": item["pk"], "sk": item["sk"]},
                    UpdateExpression="SET #s = :s, resolved_at = :now",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "resolved", ":now": iso(self.now)},
                )

    def step(self, name, fn):
        try:
            fn()
            return True
        except (BotoCoreError, ClientError) as e:
            self.errors.append({"service": name, "region": self.region, "message": error_text(e)})
            return False

    def run(self):
        c = self.clients
        days = float(os.environ.get("EC2_WINDOW_MINUTES", 10080)) / 1440
        scope = (self.account_id, self.region, self.scan_id, self.now)
        self.save_scan("running")
        ok = {
            "ec2": self.step("ec2", lambda: self.apply("ec2_instance", ec2.check_instances(
                c["ec2"], c["cloudwatch"], *scope, days=days))),
            "s3": self.step("s3", lambda: self.apply("s3_bucket", s3.check_buckets(
                c["s3"], c["cloudwatch"], self.account_id, self.region, self.scan_id, self.now,
                upload_age_days=int(os.environ.get("UPLOAD_AGE_DAYS", 7))))),
            "ebs": self.step("ebs", lambda: self.apply("ebs_volume", ebs.check_volumes(c["ec2"], *scope))),
            "rds": self.step("rds", lambda: self.apply("rds_instance", rds.check_databases(
                c["rds"], c["cloudwatch"], *scope, days=days))),
            "lambda": self.step("lambda", lambda: self.apply("lambda_function", functions.check_functions(
                c["lambda"], c["cloudwatch"], *scope, days=days,
                skip={os.environ.get("AWS_LAMBDA_FUNCTION_NAME")}))),
            "elb": self.step("elb", lambda: self.apply("load_balancer", elb.check_load_balancers(
                c["elbv2"], c["cloudwatch"], *scope, days=days))),
            "costs": self.step("costs", self.update_costs),
        }
        self.step("savings", self.update_savings)
        if not ok["costs"]:
            self.spending_check = {"outcome": "billing_failed"}
        elif not self.step("anomalies", self.update_alerts):
            self.spending_check = {"outcome": "failed"}

        if not ok["ec2"] and not ok["s3"]:
            status = "failed"
        elif self.errors:
            status = "partial"
        else:
            status = "completed"
        self.save_scan(status, completed=True)
        self.save_deployment(status)
        return status

    def update_costs(self):
        for item in costs.fetch_daily_costs(self.clients["ce"], self.account_id, self.now):
            self.table.put_item(Item=item)

    def update_savings(self):
        findings = self.query("FIND#")
        summary = costs.calculate_savings(self.account_id, findings, self.now)
        for f in findings:
            if f["status"] == "open":
                self.table.update_item(
                    Key={"pk": f["pk"], "sk": f["sk"]},
                    UpdateExpression="SET estimated_monthly_savings = :m, savings_note = :n",
                    ExpressionAttributeValues={":m": f["estimated_monthly_savings"], ":n": f["savings_note"]},
                )
        self.table.put_item(Item=summary)

    def update_alerts(self):
        start = (self.now.date() - timedelta(days=30)).isoformat()
        history = self.table.query(
            KeyConditionExpression=Key("pk").eq(self.pk) & Key("sk").between(f"COST#{start}", "COST#￿")
        )["Items"]
        result = anomalies.check_spending(self.account_id, history, self.now)
        if result["alert"]:
            try:
                # keep an existing alert as-is, so its status isn't reset on re-runs
                self.table.put_item(Item=result["alert"], ConditionExpression="attribute_not_exists(pk)")
            except self.table.meta.client.exceptions.ConditionalCheckFailedException:
                pass
        if result["outcome"] not in ("alert", "normal"):
            self.notes.append(f"spending check: {result['outcome']} ({result['note']})")
        self.spending_check = {key: value for key, value in result.items() if key != "alert"}

    def save_deployment(self, status):
        self.table.update_item(
            Key={"pk": "DEPLOYMENT", "sk": "CONFIG"},
            UpdateExpression=("SET #t = :t, account_id = :a, regions = :r, resource_types = :rt, #s = :s, "
                              "last_scan_id = :id, last_scan_at = :now, "
                              "created_at = if_not_exists(created_at, :now)"),
            ExpressionAttributeNames={"#t": "type", "#s": "status"},
            ExpressionAttributeValues={
                ":t": "deployment", ":a": self.account_id, ":r": [self.region], ":rt": RESOURCE_TYPES,
                ":s": "ready" if status != "failed" else "scan_failed", ":id": self.scan_id,
                ":now": iso(self.now),
            },
        )


def lambda_handler(event, context, clients=None, now=None):
    region = os.environ["AWS_REGION"]
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    clients = clients or {
        "ec2": boto3.client("ec2", region_name=region),
        "s3": boto3.client("s3", region_name=region),
        "cloudwatch": boto3.client("cloudwatch", region_name=region),
        "rds": boto3.client("rds", region_name=region),
        "lambda": boto3.client("lambda", region_name=region),
        "elbv2": boto3.client("elbv2", region_name=region),
        "ce": boto3.client("ce", region_name="us-east-1"),
        "sts": boto3.client("sts", region_name=region),
    }
    table = boto3.resource("dynamodb", region_name=region).Table(os.environ["DYNAMODB_TABLE_NAME"])

    account_id = clients["sts"].get_caller_identity()["Account"]
    expected = os.environ.get("EXPECTED_ACCOUNT_ID")
    if expected and expected != account_id:
        log({"status": "wrong_account", "expected": expected, "actual": account_id})
        return {"status": "wrong_account"}

    audit = Audit(table, account_id, region, now, clients)
    if not audit.lock():
        log({"status": "already_running", "source": event.get("source")})
        return {"status": "already_running"}
    try:
        status = audit.run()
    except Exception as e:
        audit.errors.append({"service": "audit", "region": region, "message": error_text(e)})
        if audit.spending_check["outcome"] == "pending":
            audit.spending_check = {"outcome": "failed"}
        try:
            audit.save_scan("failed", completed=True)
        finally:
            audit.unlock()
        raise
    audit.unlock()
    summary = {"status": status, "scan_id": audit.scan_id, **audit.counts,
               "errors": audit.errors, "notes": audit.notes}
    log(summary)
    return summary


def log(data):
    print(json.dumps(data, default=str))
