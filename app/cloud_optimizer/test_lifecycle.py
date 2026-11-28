import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

import boto3
from botocore.exceptions import ClientError
from django.test import Client, SimpleTestCase
from moto import mock_aws

from . import lifecycle, users
from .testutils import sign_in

ACCOUNT = "123456789012"
BUCKET = "team-old-logs"
EXAMPLES = Path(__file__).parent / "examples"
EXISTING = {"ID": "keep-me", "Status": "Enabled", "Filter": {"Prefix": "tmp/"}, "Expiration": {"Days": 3}}


@mock_aws
class LifecycleTests(SimpleTestCase):
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
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=BUCKET)
        self.add_finding("finding_s3_incomplete_uploads")
        self.add_finding("finding_s3_tiering")
        resource = json.loads((EXAMPLES / "resource_s3.json").read_text(), parse_float=Decimal)
        resource.update(resource_id=BUCKET, sk=f"RES#s3_bucket#us-east-1#{BUCKET}")
        self.table.put_item(Item=resource)
        sign_in(self.client)

    def add_finding(self, name, bucket=BUCKET, **changes):
        item = json.loads((EXAMPLES / f"{name}.json").read_text(), parse_float=Decimal)
        item["resource_id"] = bucket
        item["sk"] = item["sk"].replace("acme-old-logs", bucket)
        item.update(changes)
        self.table.put_item(Item=item)

    def url(self, bucket=BUCKET):
        return f"/api/s3/{bucket}/lifecycle/"

    def preview(self, rule="s3_incomplete_uploads", bucket=BUCKET):
        return self.client.get(self.url(bucket), {"rule": rule})

    def apply(self, rule="s3_incomplete_uploads", confirm="yes", request_id=None, proposal=None, bucket=BUCKET):
        if proposal is None:
            proposal = self.preview(rule, bucket).json()["proposal_hash"]
        return self.client.post(self.url(bucket), {"rule": rule, "confirm": confirm, "proposal_hash": proposal,
                                                   "request_id": request_id or str(uuid.uuid4())})

    def saved_rules(self):
        try:
            return self.s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"]
        except ClientError:
            return []

    def test_preview(self):
        body = self.preview().json()
        self.assertEqual(body["current_rules"], [])
        self.assertEqual(body["proposed_rule"]["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"], 7)
        self.assertEqual(len(body["result_rules"]), 1)
        self.assertIn("charge", body["charges"].lower())

    def test_apply_keeps_existing_rules(self):
        self.s3.put_bucket_lifecycle_configuration(Bucket=BUCKET, LifecycleConfiguration={"Rules": [EXISTING]})
        r = self.apply(request_id="req-lc-0001")
        self.assertEqual(r.status_code, 202)
        action = r.json()["action"]
        self.assertEqual(action["outcome"], "verified")
        self.assertEqual(action["action_type"], "s3_apply_lifecycle")
        self.assertEqual(sorted(x["ID"] for x in self.saved_rules()), ["cloud-optimizer-abort-uploads", "keep-me"])
        # history reader can see it, and the EC2 status check leaves it alone
        self.assertEqual(self.client.get("/api/actions/req-lc-0001/").json()["action"]["outcome"], "verified")

    def test_tiering_rule(self):
        self.assertEqual(self.apply("s3_tiering_candidate").status_code, 202)
        self.assertEqual(self.saved_rules()[0]["Transitions"][0]["StorageClass"], "INTELLIGENT_TIERING")
        activity = self.client.get("/activity/?service=s3&action=s3_tiering_candidate")
        self.assertContains(activity, "Enable Intelligent-Tiering</h3>")
        self.assertContains(activity, "Transition eligible objects")
        self.assertContains(activity, "After 0 days")
        self.assertContains(activity, "Larger than 131072 bytes")

    def test_activity_shows_saved_cleanup_configuration(self):
        item = json.loads((EXAMPLES / "resource_s3.json").read_text(), parse_float=Decimal)
        item.update(sk=f"RES#s3_bucket#us-east-1#{BUCKET}", resource_id=BUCKET, name=BUCKET)
        self.table.put_item(Item=item)
        self.assertEqual(self.apply(request_id="req-lc-activity").status_code, 202)
        page = self.client.get("/activity/?status=verified")
        self.assertContains(page, "S3 confirmed the rule was saved.")
        self.assertContains(page, "S3 processes eligible objects and uploads later.")
        self.assertContains(page, "After 7 days")
        self.assertContains(page, f'href="/s3/{BUCKET}/?request=req-lc-activity"')

    def test_declined_does_nothing(self):
        self.assertEqual(self.apply(confirm="no").status_code, 400)
        self.assertEqual(self.saved_rules(), [])

    def test_stale_proposal_is_rejected(self):
        proposal = self.preview().json()["proposal_hash"]
        self.s3.put_bucket_lifecycle_configuration(Bucket=BUCKET, LifecycleConfiguration={"Rules": [EXISTING]})
        self.assertEqual(self.apply(proposal=proposal).status_code, 409)
        self.assertEqual([x["ID"] for x in self.saved_rules()], ["keep-me"])

    def test_repeated_click(self):
        self.assertEqual(self.apply(request_id="req-lc-0002").status_code, 202)
        self.assertEqual(self.apply(request_id="req-lc-0002").json()["error"], "already_applied")
        self.assertEqual(len(self.saved_rules()), 1)

    def test_no_open_finding(self):
        self.add_finding("finding_s3_tiering", status="resolved")
        self.assertEqual(self.apply("s3_tiering_candidate", proposal="x").json()["error"], "no_open_finding")

    def test_unsupported_rule_is_rejected(self):
        self.add_finding("finding_s3_tiering", recommendation={
            "action": "s3_apply_lifecycle", "text": "x",
            "proposed_rule": {"ID": "bad", "Status": "Enabled", "Filter": {}, "Expiration": {"Days": 1}}})
        self.assertEqual(self.preview("s3_tiering_candidate").status_code, 409)

    def test_tampered_or_unknown_targets(self):
        self.assertEqual(self.preview(bucket="Bad_Name!").status_code, 404)
        self.assertEqual(self.preview(rule="s3_delete_everything").status_code, 404)
        self.add_finding("finding_s3_tiering", bucket="not-my-bucket")
        self.assertEqual(self.preview("s3_tiering_candidate", bucket="not-my-bucket").status_code, 404)

    def test_other_region_bucket(self):
        boto3.client("s3", region_name="eu-west-1").create_bucket(
            Bucket="team-eu-bucket", CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        self.add_finding("finding_s3_tiering", bucket="team-eu-bucket")
        self.assertEqual(self.preview("s3_tiering_candidate", bucket="team-eu-bucket").status_code, 404)

    def test_aws_failure_is_recorded(self):
        proposal = self.preview().json()["proposal_hash"]
        real = lifecycle.s3

        def fake():
            client = real()
            client.put_bucket_lifecycle_configuration = mock.Mock(side_effect=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutBucketLifecycleConfiguration"))
            return client
        with mock.patch.object(lifecycle, "s3", fake):
            r = self.apply(proposal=proposal)
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["action"]["request_error"], "AccessDenied")
        self.assertEqual(self.saved_rules(), [])

    def test_logged_out(self):
        self.client.post("/logout/")
        self.assertEqual(self.preview().status_code, 401)
        self.assertEqual(self.client.post(self.url(), {"rule": "s3_incomplete_uploads", "confirm": "yes"}).status_code, 401)

    def test_bucket_review_confirms_once_and_links_to_saved_result(self):
        url = f"/s3/{BUCKET}/"
        client = Client(enforce_csrf_checks=True)
        sign_in(client)
        review = client.get(url, {"review": "s3_incomplete_uploads"})
        self.assertContains(review, "All key prefixes")
        self.assertContains(review, "Days after initiation")
        self.assertEqual(self.saved_rules(), [])
        form = {"confirmation": review.context["confirmation"], "confirm": "yes"}
        self.assertEqual(client.post(url, form).status_code, 403)
        form["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        self.assertContains(client.post(url, {**form, "confirm": "no"}), "Confirm the rule", status_code=400)
        self.assertEqual(self.saved_rules(), [])

        response = client.post(url, form)
        self.assertEqual(response.status_code, 302)
        outcome = response["Location"]
        self.assertContains(client.get(outcome), "Rule saved and verified")
        self.assertContains(client.get(url), f'href="?{outcome.split("?", 1)[1]}"')
        self.assertEqual(client.post(url, form)["Location"], outcome)
        self.assertEqual(len(self.saved_rules()), 1)
        self.assertEqual(len(client.get("/api/actions/").json()["actions"]), 1)
        review = client.get(url, {"review": "s3_incomplete_uploads"})
        self.assertContains(review, "S3 already has a rule with this ID")
        self.assertNotContains(review, "Confirm and apply rule")

    def test_bucket_review_rejects_concurrent_and_stale_changes(self):
        url = f"/s3/{BUCKET}/"
        review = self.client.get(url, {"review": "s3_tiering_candidate"})
        self.assertContains(review, "Larger than 131072 bytes")
        form = {"confirmation": review.context["confirmation"], "confirm": "yes"}
        lock = {"pk": f"ACCT#{ACCOUNT}", "sk": f"S3LIFECYCLE#us-east-1#{BUCKET}"}
        self.table.put_item(Item={**lock, "expires_at": int(lifecycle.time()) + 100})
        self.assertContains(self.client.post(url, form), "Another lifecycle request", status_code=409)
        self.assertEqual(self.saved_rules(), [])
        self.table.delete_item(Key=lock)
        self.s3.put_bucket_lifecycle_configuration(Bucket=BUCKET, LifecycleConfiguration={"Rules": [EXISTING]})
        self.assertContains(self.client.post(url, form), "The proposal or lifecycle configuration changed", status_code=409)
        self.assertEqual(self.saved_rules(), [EXISTING])

        review = self.client.get(url, {"review": "s3_tiering_candidate"})
        response = self.client.post(url, {"confirmation": review.context["confirmation"], "confirm": "yes"}, follow=True)
        # Moto omits the object-size filter from lifecycle reads.
        self.assertContains(response, "Verification pending")
        self.assertEqual(self.saved_rules()[1]["Transitions"][0]["StorageClass"], "INTELLIGENT_TIERING")
        applied = response.context["action_record"]["change"]["after"]["rules"]
        with mock.patch.object(lifecycle, "current_configuration", return_value={"Rules": applied}):
            self.assertContains(self.client.get(response.redirect_chain[-1][0]), "Rule saved and verified")

    def test_bucket_outcome_can_verify_after_a_failed_read_back(self):
        url = f"/s3/{BUCKET}/"
        review = self.client.get(url, {"review": "s3_incomplete_uploads"})
        read_failed = ClientError({"Error": {"Code": "SlowDown", "Message": "try again"}},
                                  "GetBucketLifecycleConfiguration")
        with mock.patch.object(lifecycle, "current_configuration", side_effect=[{"Rules": []}, read_failed, read_failed]):
            response = self.client.post(url, {"confirmation": review.context["confirmation"], "confirm": "yes"})
            self.assertContains(self.client.get(response["Location"]), "Verification pending")

        self.assertEqual(len(self.saved_rules()), 1)
        self.assertContains(self.client.get(response["Location"]), "Rule saved and verified")

    def test_bucket_outcome_explains_access_denial(self):
        url = f"/s3/{BUCKET}/"
        review = self.client.get(url, {"review": "s3_incomplete_uploads"})
        denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "PutBucketLifecycleConfiguration")
        with mock.patch.object(lifecycle, "s3", return_value=self.s3):
            with mock.patch.object(self.s3, "put_bucket_lifecycle_configuration", side_effect=denied):
                response = self.client.post(url, {"confirmation": review.context["confirmation"], "confirm": "yes"}, follow=True)
        self.assertContains(response, "S3 denied the change")
        self.assertEqual(self.saved_rules(), [])

    def test_apply_preserves_existing_rules_and_bucket_transition_default(self):
        # Moto returns a fixed transition default. Simulate a bucket with a different default.
        prior = {**EXISTING, "Expiration": {"Date": datetime(2028, 1, 1, tzinfo=timezone.utc)}}
        configuration = {"Rules": [prior], "TransitionDefaultMinimumObjectSize": "varies_by_storage_class"}
        with mock.patch.object(lifecycle, "current_configuration", return_value=configuration):
            rule = self.preview().json()["proposed_rule"]
            with mock.patch.object(lifecycle, "s3", return_value=self.s3):
                with mock.patch.object(self.s3, "put_bucket_lifecycle_configuration") as put:
                    response = self.apply()
        self.assertEqual(response.status_code, 202)
        put.assert_called_once_with(
            Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT, LifecycleConfiguration={"Rules": [prior, rule]},
            TransitionDefaultMinimumObjectSize="varies_by_storage_class",
        )
        record = response.json()["action"]
        self.assertEqual(record["change"]["before"]["rules"][0]["Expiration"]["Date"], "2028-01-01T00:00:00Z")
