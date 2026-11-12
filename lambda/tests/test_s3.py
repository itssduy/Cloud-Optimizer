import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws

from audit import s3 as rules

ACCOUNT = "123456789012"
REGION = "us-east-1"
GIB = 1024**3


@mock_aws
class S3RuleTests(unittest.TestCase):
    def setUp(self):
        self.s3 = boto3.client("s3", region_name=REGION)
        self.cw = boto3.client("cloudwatch", region_name=REGION)
        self.now = datetime.now(timezone.utc)

    def bucket(self, name, region=REGION):
        if region == "us-east-1":
            self.s3.create_bucket(Bucket=name)
        else:
            boto3.client("s3", region_name=region).create_bucket(
                Bucket=name, CreateBucketConfiguration={"LocationConstraint": region})

    def put_storage(self, name, size, count):
        when = self.now - timedelta(hours=12)
        self.cw.put_metric_data(Namespace="AWS/S3", MetricData=[
            {"MetricName": "BucketSizeBytes", "Timestamp": when, "Value": size,
             "Dimensions": [{"Name": "BucketName", "Value": name}, {"Name": "StorageType", "Value": "StandardStorage"}]},
            {"MetricName": "NumberOfObjects", "Timestamp": when, "Value": count,
             "Dimensions": [{"Name": "BucketName", "Value": name}, {"Name": "StorageType", "Value": "AllStorageTypes"}]},
        ])

    def upload(self, name):
        # moto stamps every upload with a fixed old date, so build "now" from it
        self.s3.create_multipart_upload(Bucket=name, Key="big.bin")
        return self.s3.list_multipart_uploads(Bucket=name)["Uploads"][0]["Initiated"]

    def check(self, now=None, **kwargs):
        return rules.check_buckets(self.s3, self.cw, ACCOUNT, REGION, "scan_1", now or self.now, **kwargs)

    def by_name(self, results):
        return {r["resource"]["resource_id"]: r for r in results}

    def rules_found(self, result):
        return [f["rule"] for f in result["findings"]]

    def test_big_standard_bucket_is_tiering_candidate(self):
        self.bucket("team-logs")
        self.put_storage("team-logs", 400 * GIB, 50000)
        r = self.by_name(self.check())["team-logs"]
        self.assertEqual(r["outcome"], "finding")
        f = r["findings"][0]
        self.assertEqual(f["rule"], "s3_tiering_candidate")
        self.assertEqual(f["sk"], "FIND#s3_bucket#us-east-1#team-logs#s3_tiering_candidate")
        self.assertEqual(f["evidence"]["access_pattern"], "unknown")
        self.assertIsNone(f["estimated_monthly_savings"])

    def test_small_bucket_is_clean(self):
        self.bucket("team-small")
        self.put_storage("team-small", 2 * GIB, 100)
        self.assertEqual(self.by_name(self.check())["team-small"]["outcome"], "clean")

    def test_tiny_objects_are_not_tiering_candidates(self):
        self.bucket("team-tiny")
        self.put_storage("team-tiny", 20 * GIB, 20_000_000)
        self.assertEqual(self.by_name(self.check())["team-tiny"]["outcome"], "clean")

    def test_existing_transition_rule_skips_tiering(self):
        self.bucket("team-tiered")
        self.put_storage("team-tiered", 400 * GIB, 50000)
        self.s3.put_bucket_lifecycle_configuration(Bucket="team-tiered", LifecycleConfiguration={"Rules": [
            {"ID": "t", "Status": "Enabled", "Filter": {},
             "Transitions": [{"Days": 30, "StorageClass": "STANDARD_IA"}]}]})
        self.assertEqual(self.by_name(self.check())["team-tiered"]["outcome"], "clean")

    def test_missing_metrics_is_not_clean(self):
        self.bucket("team-new")
        # moto makes up zero-value S3 metrics, real AWS returns nothing for new buckets
        with mock.patch.object(rules, "latest_metric", return_value=(None, None)):
            r = self.by_name(self.check())["team-new"]
        self.assertEqual(r["outcome"], "insufficient_data")
        self.assertEqual(r["findings"], [])

    @mock.patch.object(rules, "latest_metric", return_value=(10, "2010-11-10T00:00:00Z"))
    def test_stale_incomplete_upload(self, _):
        self.bucket("team-uploads")
        started = self.upload("team-uploads")
        self.assertEqual(self.by_name(self.check(now=started + timedelta(days=1)))["team-uploads"]["outcome"], "clean")

        later = started + timedelta(days=8)
        r = self.by_name(self.check(now=later))["team-uploads"]
        self.assertEqual(self.rules_found(r), ["s3_incomplete_uploads"])
        self.assertEqual(r["findings"][0]["evidence"]["stale_upload_count"], 1)
        rule = r["findings"][0]["recommendation"]["proposed_rule"]
        self.assertEqual(rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"], 7)

    @mock.patch.object(rules, "latest_metric", return_value=(10, "2010-11-10T00:00:00Z"))
    def test_abort_rule_already_set(self, _):
        self.bucket("team-handled")
        started = self.upload("team-handled")
        self.s3.put_bucket_lifecycle_configuration(Bucket="team-handled", LifecycleConfiguration={"Rules": [
            {"ID": "a", "Status": "Enabled", "Filter": {},
             "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}}]})
        later = started + timedelta(days=8)
        self.assertEqual(self.by_name(self.check(now=later))["team-handled"]["outcome"], "clean")

    def test_other_region_is_skipped(self):
        self.bucket("team-here")
        self.bucket("team-there", region="eu-west-1")
        self.assertEqual(set(self.by_name(self.check())), {"team-here"})

    def test_access_denied_is_an_error(self):
        self.bucket("team-locked")
        denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "GetBucketLifecycleConfiguration")
        with mock.patch.object(rules, "lifecycle_rules", side_effect=denied):
            r = self.by_name(self.check())["team-locked"]
        self.assertEqual(r["outcome"], "error")
        self.assertIn("AccessDenied", r["note"])

    def test_does_not_change_buckets(self):
        self.bucket("team-logs")
        self.put_storage("team-logs", 400 * GIB, 50000)
        self.check()
        with self.assertRaises(ClientError):
            self.s3.get_bucket_lifecycle_configuration(Bucket="team-logs")

    def test_reads_every_page(self):
        class Paginator:
            def paginate(self):
                yield {"Buckets": [{"Name": "a"}]}
                yield {"Buckets": [{"Name": "b"}]}

        class FakeS3:
            def get_paginator(self, name):
                return Paginator()

        self.assertEqual([b["Name"] for b in rules.list_buckets(FakeS3())], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
