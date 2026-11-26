import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError
from django.test import SimpleTestCase
from moto import mock_aws

from . import users
from .testutils import sign_in

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
        sign_in(self.client)

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
        page = self.get("/status/")
        self.assertContains(page, "No saved scan")
        self.assertContains(page, "Awaiting audit report")
        self.assertContains(page, ACCOUNT)
        self.assertContains(page, "us-east-1")
        self.assertNotContains(page, "View saved findings")

    def test_overview_uses_saved_records_after_login(self):
        self.seed("scan_completed", "resource_ec2", "resource_s3",
                  "finding_ec2_idle", "finding_s3_tiering", "savings", "alert")
        today = datetime.now(timezone.utc).date().isoformat()
        self.seed("daily_cost", sk=f"COST#{today}", date=today)
        page = self.get("/")
        self.assertContains(page, "Overview")
        self.assertContains(page, "old-staging-api")
        self.assertContains(page, "acme-old-logs")
        self.assertContains(page, "$4.82")
        self.assertContains(page, "Log out")
        self.assertContains(page, 'href="/status/"')
        self.assertContains(page, 'href="/findings/">Review findings</a>')
        self.assertNotContains(page, "Sample data")
        self.assertNotContains(page, "Team users")
        page = self.get("/?service=ec2")
        self.assertContains(page, "old-staging-api")
        self.assertNotContains(page, "acme-old-logs")

    def test_status_after_scan(self):
        self.seed("deployment", "scan_completed")
        body = self.get("/api/status/").json()
        self.assertEqual(body["scan_state"], "completed")
        self.assertEqual(body["deployment"]["regions"], ["us-east-1"])
        self.assertNotIn("pk", body["latest_scan"])

    def test_old_scan_is_overdue(self):
        self.seed("scan_completed")
        self.assertTrue(self.get("/api/status/").json()["overdue"])
        self.assertContains(self.get("/status/"), "The daily scan is overdue")
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.seed("scan_completed", sk=f"SCAN#{recent}#scan_new", started_at=recent)
        self.assertFalse(self.get("/api/status/").json()["overdue"])

    def test_deployment_page_tracks_saved_scan(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.seed("deployment")
        for state in ("pending", "running", "completed", "partial", "failed"):
            with self.subTest(state=state):
                finished = state in ("completed", "partial", "failed")
                self.seed(
                    "scan_completed", status=state, started_at=None if state == "pending" else now,
                    completed_at=now if finished else None,
                )
                page = self.get("/status/")
                self.assertContains(page, state.capitalize() + "</span>")
                if state in ("completed", "partial"):
                    self.assertContains(page, "View saved findings")
                else:
                    self.assertNotContains(page, "View saved findings")
                if finished:
                    self.assertContains(page, "Finished · UTC")

    def test_deployment_page_reports_permission_issues_without_raw_errors(self):
        self.seed("scan_completed", status="partial", errors=[
            {"service": "ec2", "message": "UnauthorizedOperation: private diagnostic"},
        ])
        page = self.get("/status/")
        self.assertContains(page, "Access issue reported")
        self.assertNotContains(page, "private diagnostic")

    def test_deployment_page_requires_an_active_team_user(self):
        user = users.list_users()[0]
        users.update_user(user["user_id"], status="disabled")
        with patch("cloud_optimizer.dashboard.data.status") as status:
            self.assertRedirects(self.get("/status/"), "/login/", fetch_redirect_response=False)
            status.assert_not_called()

    def test_deployment_page_reports_configuration_and_read_failures(self):
        with patch.dict(os.environ, AWS_ACCOUNT_ID=""):
            page = self.get("/status/")
            self.assertContains(page, "AWS account is missing", status_code=503)
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "private diagnostic"}}, "Query")
        with patch("cloud_optimizer.dashboard.data.status", side_effect=error):
            page = self.get("/status/")
            self.assertContains(page, "Saved status is unavailable", status_code=503)
            self.assertNotContains(page, "private diagnostic", status_code=503)

    def test_latest_scan_is_selected_within_the_configured_region(self):
        self.seed("scan_completed")
        self.seed("scan_completed", sk="SCAN#2026-11-07T06:00:00Z#scan_other", status="failed",
                  regions=["eu-west-1"], started_at="2026-11-07T06:00:00Z")
        self.assertEqual(self.get("/api/status/").json()["scan_state"], "completed")

    def test_ec2_inventory_filters_the_configured_scope(self):
        self.seed("resource_ec2", "resource_s3", "finding_ec2_idle")
        self.seed("resource_ec2", sk="RES#ec2_instance#us-east-1#i-00000001", resource_id="i-00000001",
                  state="stopped", name="stopped-worker")
        self.seed("resource_ec2", sk="RES#ec2_instance#eu-west-1#i-00000002", resource_id="i-00000002",
                  region="eu-west-1", name="other-region")
        self.seed("resource_ec2", pk="ACCT#999999999999", name="other-account")
        page = self.get("/ec2/")
        self.assertContains(page, ACCOUNT)
        self.assertContains(page, "old-staging-api")
        self.assertContains(page, "stopped-worker")
        self.assertNotContains(page, "other-region")
        self.assertNotContains(page, "other-account")
        page = self.get("/ec2/?state=stopped&finding=none")
        self.assertContains(page, "stopped-worker")
        self.assertNotContains(page, "old-staging-api")
        self.assertContains(self.get("/ec2/?state=stopped&finding=open"), "No instances match")

    def test_ec2_detail_separates_saved_evidence_from_live_state(self):
        self.seed("resource_ec2", "scan_completed", "finding_ec2_idle")
        path = "/ec2/i-0abc123def4567890/"
        with patch("cloud_optimizer.actions.describe", return_value={"State": {"Name": "stopped"}}):
            page = self.get(path)
            self.assertContains(page, "Review start")
            self.assertContains(page, "1.2%")
            self.assertContains(page, "168 / 168")
            self.assertContains(page, "30.37")
            self.assertContains(page, "observations may be stale")
        self.seed("finding_ec2_idle", status="resolved")
        self.seed("resource_ec2", last_check={"outcome": "insufficient_data", "note": "only 4 hourly samples"})
        page = self.get(path)
        self.assertContains(page, "insufficient CPU data")
        self.assertContains(page, "No open findings are saved")
        self.assertNotContains(page, "Review start")

    def test_s3_inventory_filters_within_the_deployment(self):
        self.seed("resource_s3", "finding_s3_incomplete_uploads")
        self.seed("resource_s3", sk="RES#s3_bucket#us-east-1#empty-bucket", resource_id="empty-bucket",
                  details={"standard_size_bytes": 0, "object_count": 0, "has_lifecycle_rules": False})
        self.seed("resource_s3", sk="RES#s3_bucket#eu-west-1#other-region", resource_id="other-region",
                  region="eu-west-1")
        self.seed("resource_s3", pk="ACCT#999999999999", sk="RES#s3_bucket#us-east-1#other-account",
                  resource_id="other-account")
        page = self.get("/s3/")
        self.assertContains(page, ACCOUNT)
        self.assertContains(page, "empty-bucket")
        self.assertContains(page, "acme-old-logs")
        self.assertNotContains(page, "other-region")
        self.assertNotContains(page, "other-account")
        self.assertNotContains(self.get("/s3/?finding=open"), "empty-bucket")
        self.assertNotContains(self.get("/s3/?finding=none"), "acme-old-logs")
        self.assertEqual(self.get("/s3/other-region/").status_code, 404)
        self.assertEqual(self.get("/s3/other-account/").status_code, 404)

    def test_s3_missing_metrics_and_zero_usage_are_distinct(self):
        self.seed("resource_s3", details={})
        page = self.get("/s3/acme-old-logs/")
        self.assertContains(page, "Unavailable")
        self.assertNotContains(page, "<dd>0\u00a0bytes</dd>", html=True)
        self.assertContains(page, "No open findings are saved")
        self.seed("resource_s3", details={"standard_size_bytes": 0, "object_count": 0,
                                        "has_lifecycle_rules": False})
        self.assertContains(self.get("/s3/acme-old-logs/"), "<dd>0\u00a0bytes</dd>", html=True)

    def test_s3_saved_rule_review_expires_with_the_finding(self):
        self.seed("resource_s3", "scan_completed", "finding_s3_incomplete_uploads", "finding_s3_tiering")
        page = self.get("/s3/acme-old-logs/?review=s3_incomplete_uploads")
        self.assertContains(page, "DaysAfterInitiation")
        self.assertContains(page, "acme-old-logs")
        self.assertContains(page, "Applying lifecycle rules is unavailable")
        self.assertContains(page, "observations may be stale")
        self.assertEqual(self.client.post("/s3/acme-old-logs/", {"confirm": "yes"}).status_code, 405)
        self.seed("finding_s3_incomplete_uploads", status="resolved")
        self.assertEqual(self.get("/s3/acme-old-logs/?review=s3_incomplete_uploads").status_code, 404)
        self.assertContains(self.get("/s3/acme-old-logs/?review=s3_tiering_candidate"), "INTELLIGENT_TIERING")
        self.assertContains(self.get("/s3/acme-old-logs/"), "Savings estimate unavailable")

    def test_s3_pages_require_an_active_team_user(self):
        user = users.list_users()[0]
        users.update_user(user["user_id"], status="disabled")
        with patch("cloud_optimizer.s3_pages.data.status") as status:
            self.assertRedirects(self.get("/s3/"), "/login/", fetch_redirect_response=False)
            self.assertRedirects(self.get("/s3/acme-old-logs/"), "/login/", fetch_redirect_response=False)
            status.assert_not_called()

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

    def test_findings_page_shows_scoped_advice_and_resource_links(self):
        self.seed("scan_completed", "resource_ec2", "resource_s3", "finding_s3_tiering")
        self.seed("finding_ec2_idle", savings_note="Compute at the on-demand rate for 730 hours")
        self.seed("finding_s3_incomplete_uploads", pk="ACCT#999999999999", reason="Different account finding")
        self.seed("finding_s3_incomplete_uploads", sk="FIND#s3_bucket#eu-west-1#other-bucket#s3_incomplete_uploads",
                  resource_id="other-bucket", region="eu-west-1", reason="Different region finding")

        page = self.get("/findings/")
        for text in (ACCOUNT, "us-east-1", "Last observed", "168 of 168", "USD 30.37 / month", "730 hours",
                     "Intelligent-Tiering", "monitoring fee", "Monthly savings estimate unavailable"):
            self.assertContains(page, text)
        self.assertContains(page, 'href="/ec2/i-0abc123def4567890/">old-staging-api</a>')
        self.assertContains(page, 'href="/s3/acme-old-logs/">acme-old-logs</a>')
        self.assertNotContains(page, "Different account finding")
        self.assertNotContains(page, "Different region finding")
        filtered = self.get("/findings/?service=s3&status=open")
        self.assertContains(filtered, 'href="/findings/?service=s3&amp;status=open">Reload saved results</a>')
        self.assertNotContains(filtered, "old-staging-api")
        self.assertContains(self.get("/findings/?service=ec2&status=resolved"), "No resolved findings match these filters.")

    def test_later_scan_updates_one_finding_and_can_resolve_it(self):
        self.seed("scan_completed", "finding_ec2_idle")
        self.assertContains(self.get("/findings/"), "Average CPU was 1.2% over the last 7 days")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.seed("scan_completed", sk=f"SCAN#{today}#second-scan", scan_id="second-scan", started_at=today,
                  completed_at=today, finding_count=1)
        self.seed("finding_ec2_idle", scan_id="second-scan", reason="Later scan confirmed low CPU",
                  last_detected_at=today)
        changed = self.get("/findings/")
        self.assertContains(changed, "Later scan confirmed low CPU", count=1)
        self.assertNotContains(changed, "From an earlier scan")

        self.seed("finding_ec2_idle", status="resolved", resolved_at=today, reason="Later scan confirmed low CPU")
        self.assertNotContains(self.get("/findings/"), "Later scan confirmed low CPU")
        resolved = self.get("/findings/?service=ec2&status=resolved")
        self.assertContains(resolved, "Later scan confirmed low CPU", count=1)
        self.assertContains(resolved, "Previous recommendation")
        self.assertContains(resolved, "Previous estimate")
        self.assertNotContains(resolved, 'href="/ec2/i-0abc123def4567890/"')

    def test_findings_page_distinguishes_empty_and_incomplete_scans(self):
        self.assertContains(self.get("/findings/"), "No saved scan")
        self.seed("scan_completed", finding_count=0)
        stale = self.get("/findings/")
        self.assertContains(stale, "The daily scan is overdue")
        self.assertNotContains(stale, "No open findings in the latest completed scan")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        scan = {"sk": f"SCAN#{now}#latest", "scan_id": "latest", "started_at": now,
                "completed_at": now, "finding_count": 0}
        for state, missing, message in (
            ("completed", 0, "No open findings in the latest completed scan"),
            ("completed", 1, "1 resource check had insufficient data"),
            ("partial", 0, "Some checks did not finish"),
            ("failed", 0, "Failed</span>"),
        ):
            with self.subTest(state=state, missing=missing):
                self.seed("scan_completed", status=state, insufficient_data_count=missing, **scan)
                self.assertContains(self.get("/findings/"), message)

        self.seed("finding_ec2_idle", evidence={}, estimated_monthly_savings=None)
        old = self.get("/findings/")
        self.assertContains(old, "From an earlier scan")
        self.assertContains(old, "Evidence is incomplete")
        self.assertContains(old, "Monthly savings estimate unavailable")

    def test_findings_page_reports_read_error(self):
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "private diagnostic"}}, "Query")
        with patch("cloud_optimizer.dashboard.data.findings", side_effect=error):
            page = self.get("/findings/")
        self.assertContains(page, "Saved findings are temporarily unavailable", status_code=503)
        self.assertNotContains(page, "private diagnostic", status_code=503)

    def test_findings_page_requires_login_and_only_reads(self):
        self.assertEqual(self.client.post("/findings/").status_code, 405)
        self.client.post("/logout/")
        with patch("cloud_optimizer.dashboard.data.status") as status:
            self.assertRedirects(self.get("/findings/"), "/login/", fetch_redirect_response=False)
            status.assert_not_called()

    def test_costs_and_missing_savings(self):
        today = datetime.now(timezone.utc).date().isoformat()
        self.seed("daily_cost", sk=f"COST#{today}", date=today)
        body = self.get("/api/costs/").json()
        self.assertEqual(body["costs"][0]["total"]["amount"], 4.82)
        self.assertIsNone(body["savings"])

    def seed_billing_day(self, days_ago, services, estimated=False, account=ACCOUNT):
        date = (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat()
        by_service = {
            name: {"amount": Decimal(amount), "currency": "USD", "estimated": estimated}
            for name, amount in services.items()
        }
        total = {"amount": sum(money["amount"] for money in by_service.values()),
                 "currency": "USD", "estimated": estimated}
        self.seed("daily_cost", pk=f"ACCT#{account}", sk=f"COST#{date}", date=date,
                  total=total, by_service=by_service)

    def test_spending_filters_saved_dates_and_service_amounts(self):
        compute = "Amazon Elastic Compute Cloud - Compute"
        self.seed_billing_day(1, {compute: "10.10"}, estimated=True)
        self.seed_billing_day(2, {compute: "0"})
        self.seed_billing_day(5, {"Amazon Simple Storage Service": "-1.20"})
        self.seed_billing_day(12, {"AWS Lambda": "4"})
        self.seed_billing_day(0, {"Unfinished billing day": "100"})
        self.seed_billing_day(1, {"Different account service": "100"}, account="999999999999")

        week = self.client.get("/spending/", {"days": 7})
        for text in ("USD 8.90", "USD 0.00", "USD -1.20", "3 of 7 days", "Includes estimated billing",
                     "Some billing days are missing", "AWS Cost Explorer", "Billing fetched", "All regions"):
            self.assertContains(week, text)
        self.assertNotContains(week, "Unfinished billing day")
        self.assertNotContains(week, "Different account service")
        self.assertContains(self.get("/spending/"), "USD 12.90")

        selected = self.client.get("/spending/", {"days": 7, "service": compute})
        self.assertContains(selected, "USD 10.10")
        self.assertContains(selected, "No service entry")
        self.assertNotContains(selected, "USD 8.90")
        older = self.client.get("/spending/", {"days": 7, "service": "AWS Lambda"})
        self.assertContains(older, "No amounts are saved for this selection")
        self.assertContains(older, 'value="AWS Lambda" selected')

    def test_spending_shows_projections_and_unpriced_findings(self):
        self.seed("savings", "resource_ec2", "resource_s3", "finding_ec2_idle")
        self.seed("finding_s3_tiering", savings_note="Depends on future access patterns")
        page = self.get("/spending/")
        for text in ("No billing data is saved", "USD 30.37", "estimated / month", "730 hours",
                     "Achieved savings · Unavailable", "1 finding without a dollar estimate",
                     "Depends on future access patterns", "transition request charge"):
            self.assertContains(page, text)
        self.assertContains(page, 'href="/ec2/i-0abc123def4567890/"')
        self.assertContains(page, 'href="/s3/acme-old-logs/"')

        self.seed("savings", finding_count=1, missing_estimate_count=1,
                  total_monthly={"amount": Decimal("0"), "currency": "USD"})
        unpriced = self.get("/spending/")
        self.assertContains(unpriced, "Monthly estimate unavailable")
        self.assertNotContains(unpriced, "USD 0.00")

    def test_spending_reports_the_latest_billing_failure_and_saved_read_errors(self):
        self.seed_billing_day(2, {"Amazon Simple Storage Service": "1.20"})
        self.seed("scan_completed", status="partial", errors=[{"service": "costs", "message": "private diagnostic"}])
        page = self.get("/spending/")
        self.assertContains(page, "The latest billing fetch failed")
        self.assertContains(page, "USD 1.20")
        self.assertNotContains(page, "private diagnostic")

        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "private diagnostic"}}, "Query")
        with patch("cloud_optimizer.spending.data.costs", side_effect=error):
            page = self.get("/spending/")
        self.assertContains(page, "Saved spending data is temporarily unavailable", status_code=503)
        self.assertNotContains(page, "private diagnostic", status_code=503)

    def test_spending_requires_login_and_only_reads(self):
        self.assertContains(self.get("/"), 'href="/spending/">View spending and savings</a>')
        self.assertEqual(self.client.post("/spending/").status_code, 405)
        self.client.post("/logout/")
        with patch("cloud_optimizer.spending.data.costs") as costs:
            self.assertRedirects(self.get("/spending/"), "/login/", fetch_redirect_response=False)
            costs.assert_not_called()

    def test_alerts_and_actions(self):
        self.seed("alert", "action_ec2_stop")
        self.assertEqual(self.get("/api/alerts/").json()["alerts"][0]["alert_type"], "spend_anomaly")
        self.assertEqual(self.get("/api/actions/").json()["actions"][0]["outcome"], "verified")

    def test_alert_list_and_detail_show_only_scoped_records(self):
        other_id = "2026-11-06-total"
        self.seed("alert")
        self.seed("alert", pk="ACCT#999999999999", sk="ALERT#2026-11-06#total",
                  alert_id=other_id, explanation="Other account alert")

        listing = self.get("/alerts/")
        self.assertContains(listing, ACCOUNT)
        self.assertContains(listing, 'href="/alerts/2026-11-05-total/"')
        self.assertContains(listing, "Earlier billing day")
        self.assertContains(listing, "Estimated billing")
        self.assertNotContains(listing, "Other account alert")
        self.assertEqual(self.get(f"/alerts/{other_id}/").status_code, 404)

        detail = self.get("/alerts/2026-11-05-total/")
        for text in ("USD 4.82", "USD 2.10", "14-day average", "AWS Cost Explorer", "Billing fetched",
                     "Amazon Elastic Compute Cloud - Compute", "View service spending", "Observed at detection"):
            self.assertContains(detail, text)
        self.assertContains(detail, 'href="/findings/?service=ec2"')
        self.assertContains(detail, 'href="/spending/?days=7&amp;day=2026-11-05"')

    def test_alert_spending_link_preserves_the_historical_day_and_service(self):
        day = (datetime.now(timezone.utc).date() - timedelta(days=35)).isoformat()
        service = "Amazon Simple Storage Service"
        self.seed_billing_day(35, {service: "10.50"})
        self.seed_billing_day(36, {"Amazon EC2": "6.00"})
        self.seed_billing_day(34, {service: "25.00"})
        page = self.client.get("/spending/", {"days": 7, "day": day, "service": service})
        self.assertEqual(page.context["end"].isoformat(), day)
        self.assertContains(page, "USD 10.50")
        self.assertContains(page, f'name="day" value="{day}"')
        self.assertNotContains(page, "USD 25.00")
        empty = self.client.get("/spending/", {"days": 7, "day": day, "service": "Amazon CloudWatch"})
        self.assertContains(empty, "Spending · Amazon CloudWatch")
        self.assertContains(empty, "No amounts are saved for this selection.")
        older_day = (datetime.now(timezone.utc).date() - timedelta(days=405)).isoformat()
        older = self.client.get("/spending/", {"days": 7, "day": older_day})
        self.assertEqual(older.context["end"].isoformat(), older_day)
        self.assertContains(older, "No billing data is saved for this period.")
        self.assertEqual(self.client.get("/spending/", {"day": "2026-02-30"}).status_code, 200)

    def test_alerts_distinguish_the_latest_saved_check_states(self):
        page = self.get("/alerts/")
        self.assertContains(page, "No saved check")
        self.assertNotContains(page, "No anomaly detected")

        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        check_day = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
        cases = (
            ("normal", "No anomaly detected"),
            ("no_data", "Billing data unavailable"),
            ("insufficient_history", "Insufficient billing history"),
            ("stale", "Billing data is stale"),
            ("billing_failed", "Billing fetch failed"),
            ("failed", "Spending check failed"),
        )
        for outcome, label in cases:
            with self.subTest(outcome=outcome):
                check = {"outcome": outcome, "date": check_day, "billing_estimated": True,
                         "baseline_days": 8, "baseline_window_days": 14}
                self.seed("scan_completed", sk=f"SCAN#{today}#test", started_at=today,
                          completed_at=today, spending_check=check)
                page = self.get("/alerts/")
                self.assertContains(page, label)
                if outcome == "normal":
                    self.assertContains(page, "estimated charges")
                    self.assertContains(page, "Newer billing days are still unavailable")
                    self.assertContains(page, "Only 8 of 14 baseline days were available")
                else:
                    self.assertNotContains(page, "No anomaly detected")

    def test_alerts_use_legacy_notes_and_errors_without_assuming_a_clean_check(self):
        self.seed("scan_completed")
        page = self.get("/alerts/")
        self.assertContains(page, "Check result unavailable")
        self.assertContains(page, "The latest audit is overdue")
        self.assertNotContains(page, "No anomaly detected")
        self.seed("scan_completed", notes=["spending check: insufficient_history (only 2 of 7 days of history needed)"])
        self.assertContains(self.get("/alerts/"), "Insufficient billing history")
        self.seed("scan_completed", errors=[{"service": "anomalies", "message": "private diagnostic"}])
        page = self.get("/alerts/")
        self.assertContains(page, "Spending check failed")
        self.assertNotContains(page, "private diagnostic")

    def test_alert_read_errors_and_signed_out_requests(self):
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "private diagnostic"}}, "Query")
        with patch("cloud_optimizer.alert_pages.data.alerts", side_effect=error):
            page = self.get("/alerts/")
        self.assertContains(page, "Saved alerts are temporarily unavailable", status_code=503)
        self.assertNotContains(page, "private diagnostic", status_code=503)
        self.assertEqual(self.client.post("/alerts/").status_code, 405)
        self.client.post("/logout/")
        self.assertRedirects(self.get("/alerts/"), "/login/", fetch_redirect_response=False)
        self.assertRedirects(self.get("/alerts/2026-11-05-total/"), "/login/", fetch_redirect_response=False)

    def test_store_down_is_503(self):
        self.table.delete()
        self.assertEqual(self.get("/api/findings/").status_code, 503)
        page = self.get("/status/")
        self.assertContains(page, "Saved status is unavailable", status_code=503)
        self.assertNotContains(page, ACCOUNT, status_code=503)
        self.assertContains(self.get("/ec2/"), "EC2 data is unavailable", status_code=503)
        self.assertContains(self.get("/s3/"), "Saved bucket data is unavailable", status_code=503)

    def test_post_not_allowed(self):
        self.assertEqual(self.client.post("/api/findings/").status_code, 405)
