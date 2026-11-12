import unittest
from datetime import datetime, timedelta, timezone

import boto3
from moto import mock_aws

from audit import ec2 as rules

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
ACCOUNT = "123456789012"
REGION = "us-east-1"


@mock_aws
class Ec2RuleTests(unittest.TestCase):
    def setUp(self):
        self.ec2 = boto3.client("ec2", region_name=REGION)
        self.cw = boto3.client("cloudwatch", region_name=REGION)
        self.ami = self.ec2.describe_images()["Images"][0]["ImageId"]

    def launch(self, name="web"):
        return self.ec2.run_instances(
            ImageId=self.ami, MinCount=1, MaxCount=1, InstanceType="t3.medium",
            TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": name}]}],
        )["Instances"][0]["InstanceId"]

    def put_cpu(self, instance_id, value, hours=7 * 24):
        for start in range(0, hours, 20):
            self.cw.put_metric_data(Namespace="AWS/EC2", MetricData=[
                {"MetricName": "CPUUtilization",
                 "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
                 "Timestamp": NOW - timedelta(hours=h, minutes=30),
                 "Value": value}
                for h in range(start, min(start + 20, hours))
            ])

    def check(self, **kwargs):
        return rules.check_instances(self.ec2, self.cw, ACCOUNT, REGION, "scan_1", NOW, **kwargs)

    def by_id(self, results):
        return {r["resource"]["resource_id"]: r for r in results}

    def test_low_cpu_is_a_finding(self):
        i = self.launch()
        self.put_cpu(i, 1.2)
        r = self.by_id(self.check())[i]
        self.assertEqual(r["outcome"], "finding")
        f = r["findings"][0]
        self.assertEqual(f["sk"], f"FIND#ec2_instance#{REGION}#{i}#ec2_idle_cpu")
        self.assertEqual(f["evidence"]["average"], 1.2)
        self.assertEqual(f["evidence"]["datapoints"], 168)
        self.assertEqual(f["evidence"]["instance_type"], "t3.medium")
        self.assertIsNone(f["estimated_monthly_savings"])

    def test_high_cpu_is_clean(self):
        i = self.launch()
        self.put_cpu(i, 40)
        self.assertEqual(self.by_id(self.check())[i]["outcome"], "clean")

    def test_exactly_threshold_is_clean(self):
        i = self.launch()
        self.put_cpu(i, 5.0)
        self.assertEqual(self.by_id(self.check())[i]["outcome"], "clean")

    def test_no_metrics_is_not_clean(self):
        i = self.launch()
        r = self.by_id(self.check())[i]
        self.assertEqual(r["outcome"], "insufficient_data")
        self.assertEqual(r["findings"], [])

    def test_partial_metrics_is_not_clean(self):
        i = self.launch()
        self.put_cpu(i, 1.0, hours=48)
        self.assertEqual(self.by_id(self.check())[i]["outcome"], "insufficient_data")

    def test_stopped_instance_is_ineligible(self):
        i = self.launch()
        self.put_cpu(i, 0.5)
        self.ec2.stop_instances(InstanceIds=[i])
        r = self.by_id(self.check())[i]
        self.assertEqual(r["outcome"], "ineligible")
        self.assertIn("stopped", r["note"])

    def test_spot_and_asg_are_ineligible(self):
        running = {"State": {"Name": "running"}}
        self.assertEqual(rules.ineligible_reason({**running, "InstanceLifecycle": "spot"}), "spot instance")
        self.assertIn("auto scaling", rules.ineligible_reason(
            {**running, "Tags": [{"Key": "aws:autoscaling:groupName", "Value": "g"}]}))

    def test_threshold_is_configurable(self):
        i = self.launch()
        self.put_cpu(i, 8)
        self.assertEqual(self.by_id(self.check(threshold=10))[i]["outcome"], "finding")

    def test_repeat_scan_gives_same_key(self):
        i = self.launch()
        self.put_cpu(i, 1)
        a = self.by_id(self.check())[i]["findings"][0]
        b = self.by_id(self.check())[i]["findings"][0]
        self.assertEqual((a["pk"], a["sk"]), (b["pk"], b["sk"]))

    def test_reads_every_page(self):
        class Paginator:
            def paginate(self):
                yield {"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}]}
                yield {"Reservations": [{"Instances": [{"InstanceId": "i-2"}]}]}

        class FakeEc2:
            def get_paginator(self, name):
                return Paginator()

        self.assertEqual([i["InstanceId"] for i in rules.list_instances(FakeEc2())], ["i-1", "i-2"])


if __name__ == "__main__":
    unittest.main()
