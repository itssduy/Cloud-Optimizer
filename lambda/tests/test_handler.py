import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from moto import mock_aws

import handler

NOW = datetime(2026, 11, 15, 6, 0, tzinfo=timezone.utc)
REGION = "us-east-1"
PK = "ACCT#123456789012"


class FakeCE:
    def __init__(self, fail=False):
        self.fail = fail

    def get_cost_and_usage(self, **kwargs):
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no ce"}}, "GetCostAndUsage")
        days = [NOW.date() - timedelta(days=i) for i in range(1, 16)]
        return {"ResultsByTime": [
            {"TimePeriod": {"Start": d.isoformat(), "End": d.isoformat()}, "Estimated": False,
             "Groups": [{"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "2", "Unit": "USD"}}}]}
            for d in days]}


@mock_aws
class HandlerTests(unittest.TestCase):
    def setUp(self):
        os.environ.update(AWS_REGION=REGION, DYNAMODB_TABLE_NAME="test")
        os.environ.pop("EXPECTED_ACCOUNT_ID", None)
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="test",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.table = boto3.resource("dynamodb", region_name=REGION).Table("test")
        self.ec2 = boto3.client("ec2", region_name=REGION)
        self.cw = boto3.client("cloudwatch", region_name=REGION)
        self.s3 = boto3.client("s3", region_name=REGION)
        ami = self.ec2.describe_images()["Images"][0]["ImageId"]
        self.instance = self.ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1,
                                               InstanceType="t3.medium")["Instances"][0]["InstanceId"]

    def clients(self, ce=None, **overrides):
        c = {"ec2": self.ec2, "s3": self.s3, "cloudwatch": self.cw, "ce": ce or FakeCE(),
             "sts": boto3.client("sts", region_name=REGION)}
        c.update(overrides)
        return c

    def put_cpu(self, value, now=NOW):
        points = [{"MetricName": "CPUUtilization", "Value": value,
                   "Dimensions": [{"Name": "InstanceId", "Value": self.instance}],
                   "Timestamp": now - timedelta(hours=h, minutes=30)} for h in range(168)]
        for i in range(0, len(points), 20):
            self.cw.put_metric_data(Namespace="AWS/EC2", MetricData=points[i:i + 20])

    def run_audit(self, now=NOW, **kwargs):
        return handler.lambda_handler({"source": "aws.events"}, None, clients=self.clients(**kwargs), now=now)

    def items(self, prefix):
        return self.table.query(KeyConditionExpression=Key("pk").eq(PK) & Key("sk").begins_with(prefix))["Items"]

    def test_idle_instance_scan(self):
        self.put_cpu(1.0)
        result = self.run_audit()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["findings"], 1)

        finding = self.items("FIND#")[0]
        self.assertEqual(finding["rule"], "ec2_idle_cpu")
        self.assertEqual(str(finding["estimated_monthly_savings"]["amount"]), "30.37")
        self.assertEqual(self.items("RES#ec2_instance")[0]["last_check"]["outcome"], "finding")

        scan = self.items("SCAN#")[0]
        self.assertEqual(scan["status"], "completed")
        self.assertIsNotNone(scan["completed_at"])
        self.assertEqual(len(self.items("COST#")), 15)
        self.assertEqual(self.items("SAVINGS")[0]["finding_count"], 1)
        deployment = self.table.get_item(Key={"pk": "DEPLOYMENT", "sk": "CONFIG"})["Item"]
        self.assertEqual(deployment["last_scan_id"], result["scan_id"])
        self.assertEqual(deployment["status"], "ready")
        self.assertIsNone(self.table.get_item(Key={"pk": PK, "sk": "SCANLOCK"}).get("Item"))

    def test_clean_scan(self):
        self.put_cpu(50)
        result = self.run_audit()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.items("FIND#"), [])
        self.assertEqual(self.items("RES#ec2_instance")[0]["last_check"]["outcome"], "clean")
        check = self.items("SCAN#")[0]["spending_check"]
        self.assertEqual(check["outcome"], "normal")
        self.assertEqual(check["baseline_days"], 14)
        self.assertFalse(check["billing_estimated"])

    def test_spike_check_and_its_alert_are_saved_without_duplicates(self):
        cost_explorer = mock.Mock()
        response = FakeCE().get_cost_and_usage()
        latest = response["ResultsByTime"][0]
        latest["Estimated"] = True
        latest["Groups"][0]["Metrics"]["UnblendedCost"]["Amount"] = "8"
        cost_explorer.get_cost_and_usage.return_value = response

        self.run_audit(ce=cost_explorer)
        self.run_audit(now=NOW + timedelta(hours=1), ce=cost_explorer)
        alert, = self.items("ALERT#")
        check = self.items("SCAN#")[-1]["spending_check"]
        self.assertEqual(check["outcome"], "alert")
        self.assertEqual(check["date"], alert["date"])
        self.assertEqual(check["billing_fetched_at"], handler.iso(NOW + timedelta(hours=1)))
        self.assertTrue(check["billing_estimated"])
        self.assertEqual(str(alert["actual"]["amount"]), "8.00")
        self.assertEqual(str(alert["expected"]["amount"]), "2.00")
        self.assertEqual(alert["created_at"], handler.iso(NOW))

    def test_insufficient_history_saves_coverage(self):
        cost_explorer = mock.Mock()
        cost_explorer.get_cost_and_usage.return_value = {"ResultsByTime": FakeCE().get_cost_and_usage()["ResultsByTime"][:3]}
        self.run_audit(ce=cost_explorer)
        check = self.items("SCAN#")[0]["spending_check"]
        self.assertEqual(check["outcome"], "insufficient_history")
        self.assertEqual(check["baseline_days"], 2)
        self.assertEqual(check["baseline_window_days"], 14)
        self.assertEqual(self.items("ALERT#"), [])

    def test_no_metrics_is_counted_not_clean(self):
        result = self.run_audit()
        self.assertEqual(result["insufficient_data"], 1)
        self.assertEqual(self.items("RES#ec2_instance")[0]["last_check"]["outcome"], "insufficient_data")

    def test_repeat_run_keeps_one_finding_and_first_detected(self):
        self.put_cpu(1.0)
        self.run_audit()
        later = NOW + timedelta(hours=1)
        self.run_audit(now=later)
        findings = self.items("FIND#")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["first_detected_at"], handler.iso(NOW))
        self.assertEqual(findings[0]["last_detected_at"], handler.iso(later))
        self.assertEqual(len(self.items("SCAN#")), 2)

    def test_finding_resolves_when_fixed(self):
        self.put_cpu(1.0)
        self.run_audit()
        self.cw = boto3.client("cloudwatch", region_name=REGION)
        later = NOW + timedelta(days=8)
        self.put_cpu(60, now=later)
        self.run_audit(now=later)
        finding = self.items("FIND#")[0]
        self.assertEqual(finding["status"], "resolved")
        self.assertEqual(finding["resolved_at"], handler.iso(later))

    def test_overlapping_run_is_skipped(self):
        self.table.put_item(Item={"pk": PK, "sk": "SCANLOCK", "scan_id": "other",
                                  "until": handler.iso(NOW + timedelta(minutes=5))})
        self.assertEqual(self.run_audit()["status"], "already_running")
        self.assertEqual(self.items("SCAN#"), [])

    def test_expired_lock_is_taken_over(self):
        self.table.put_item(Item={"pk": PK, "sk": "SCANLOCK", "scan_id": "old",
                                  "until": handler.iso(NOW - timedelta(minutes=1))})
        self.assertEqual(self.run_audit()["status"], "completed")

    def test_billing_failure_is_partial(self):
        self.put_cpu(1.0)
        result = self.run_audit(ce=FakeCE(fail=True))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"][0]["service"], "costs")
        self.assertEqual(len(self.items("FIND#")), 1)
        self.assertEqual(self.items("SCAN#")[0]["spending_check"]["outcome"], "billing_failed")

    def test_detector_read_failure_records_failed_check(self):
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "no cost history"}}, "Query")
        with mock.patch.object(handler.Audit, "update_alerts", side_effect=error):
            result = self.run_audit()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.items("SCAN#")[0]["spending_check"]["outcome"], "failed")

    def test_s3_denied_keeps_old_s3_findings(self):
        self.table.put_item(Item={"pk": PK, "sk": "FIND#s3_bucket#us-east-1#team-logs#s3_tiering_candidate",
                                  "resource_type": "s3_bucket", "resource_id": "team-logs",
                                  "region": REGION, "rule": "s3_tiering_candidate", "status": "open"})
        denied = mock.Mock()
        denied.get_paginator.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListBuckets")
        result = self.run_audit(s3=denied)
        self.assertEqual(result["status"], "partial")
        self.assertIn("AccessDenied", result["errors"][0]["message"])
        s3_finding = [f for f in self.items("FIND#") if f["resource_type"] == "s3_bucket"][0]
        self.assertEqual(s3_finding["status"], "open")

    def test_everything_failing_is_failed(self):
        denied = mock.Mock()
        denied.get_paginator.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "x")
        result = self.run_audit(ec2=denied, s3=denied)
        self.assertEqual(result["status"], "failed")
        deployment = self.table.get_item(Key={"pk": "DEPLOYMENT", "sk": "CONFIG"})["Item"]
        self.assertEqual(deployment["status"], "scan_failed")

    def test_wrong_account_does_nothing(self):
        os.environ["EXPECTED_ACCOUNT_ID"] = "999999999999"
        self.assertEqual(self.run_audit()["status"], "wrong_account")
        self.assertEqual(self.items("SCAN#"), [])

    def test_crash_marks_scan_failed(self):
        with mock.patch.object(handler.Audit, "save_deployment", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_audit()
        scan = self.items("SCAN#")[0]
        self.assertEqual(scan["status"], "failed")
        self.assertIsNone(self.table.get_item(Key={"pk": PK, "sk": "SCANLOCK"}).get("Item"))

    def test_does_not_touch_instances(self):
        self.put_cpu(1.0)
        self.run_audit()
        state = self.ec2.describe_instances()["Reservations"][0]["Instances"][0]["State"]["Name"]
        self.assertEqual(state, "running")


if __name__ == "__main__":
    unittest.main()
