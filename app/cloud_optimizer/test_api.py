import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
from django.test import SimpleTestCase
from moto import mock_aws

from . import users

EXAMPLES = Path(__file__).parent / "examples"
ACCOUNT = "123456789012"


@mock_aws
class ApiTests(SimpleTestCase):
    def setUp(self):
        os.environ.update(DYNAMODB_TABLE_NAME="test", AWS_REGION="us-east-1", AWS_ACCOUNT_ID=ACCOUNT)
        os.environ.pop("DYNAMODB_ENDPOINT_URL", None)
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="test",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.table = users.table()
        users.create_user("m@team.com", "M", "correct-horse-9")
        self.client.post("/login/", {"email": "m@team.com", "password": "correct-horse-9"})

    def seed(self, *names, **overrides):
        for name in names:
            item = json.loads((EXAMPLES / f"{name}.json").read_text(), parse_float=Decimal)
            item.update(overrides)
            self.table.put_item(Item=item)

    def get(self, url):
        return self.client.get(url)

    def test_logged_out_gets_401(self):
        self.client.post("/logout/")
        for url in ("/api/status/", "/api/resources/", "/api/findings/", "/api/costs/"):
            self.assertEqual(self.get(url).status_code, 401, url)

    def test_disabled_user_gets_401(self):
        user = users.list_users()[0]
        users.update_user(user["user_id"], status="disabled")
        self.assertEqual(self.get("/api/findings/").status_code, 401)

    def test_status_before_any_scan(self):
        body = self.get("/api/status/").json()
        self.assertEqual(body["scan_state"], "no_scan")
        self.assertIsNone(body["latest_scan"])
        self.assertEqual(body["account_id"], ACCOUNT)

    def test_status_after_scan(self):
        self.seed("deployment", "scan_completed")
        body = self.get("/api/status/").json()
        self.assertEqual(body["scan_state"], "completed")
        self.assertEqual(body["deployment"]["regions"], ["us-east-1"])
        self.assertNotIn("pk", body["latest_scan"])

    def test_resources_and_detail(self):
        self.seed("resource_ec2", "resource_s3", "finding_ec2_idle", "action_ec2_stop")
        self.assertEqual(len(self.get("/api/resources/").json()["resources"]), 2)
        self.assertEqual(len(self.get("/api/resources/?type=s3_bucket").json()["resources"]), 1)
        body = self.get("/api/resources/ec2_instance/i-0abc123def4567890/").json()
        self.assertEqual(body["resource"]["name"], "old-staging-api")
        self.assertEqual(body["findings"][0]["rule"], "ec2_idle_cpu")
        self.assertEqual(body["actions"][0]["action_type"], "ec2_stop")

    def test_bad_or_unknown_resource_is_404(self):
        self.seed("resource_ec2")
        for url in ("/api/resources/ec2_instance/i-doesnotexist/",
                    "/api/resources/rds_instance/db-1/",
                    "/api/resources/ec2_instance/..%2F..%2Fadmin/",
                    "/api/resources/?type=bogus"):
            self.assertEqual(self.get(url).status_code, 404, url)

    def test_other_account_data_is_invisible(self):
        self.seed("resource_ec2", pk="ACCT#999999999999")
        self.assertEqual(self.get("/api/resources/").json()["resources"], [])

    def test_other_region_data_is_invisible(self):
        self.seed("resource_ec2", region="eu-west-1")
        self.assertEqual(self.get("/api/resources/").json()["resources"], [])

    def test_findings_filter(self):
        self.seed("finding_ec2_idle", "finding_s3_tiering")
        self.seed("finding_s3_incomplete_uploads", status="resolved")
        self.assertEqual(len(self.get("/api/findings/").json()["findings"]), 2)
        self.assertEqual(len(self.get("/api/findings/?status=all").json()["findings"]), 3)

    def test_costs_and_missing_savings(self):
        today = datetime.now(timezone.utc).date().isoformat()
        self.seed("daily_cost", sk=f"COST#{today}", date=today)
        body = self.get("/api/costs/").json()
        self.assertEqual(body["costs"][0]["total"]["amount"], 4.82)
        self.assertIsNone(body["savings"])

    def test_alerts_and_actions(self):
        self.seed("alert", "action_ec2_stop")
        self.assertEqual(self.get("/api/alerts/").json()["alerts"][0]["alert_type"], "spend_anomaly")
        self.assertEqual(self.get("/api/actions/").json()["actions"][0]["outcome"], "verified")

    def test_store_down_is_503(self):
        self.table.delete()
        self.assertEqual(self.get("/api/findings/").status_code, 503)

    def test_post_not_allowed(self):
        self.assertEqual(self.client.post("/api/findings/").status_code, 405)
