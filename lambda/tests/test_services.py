import io
import json
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from moto import mock_aws

from audit import costs, ebs, elb, functions, rds

ACCOUNT = "123456789012"
REGION = "us-east-1"
# resources created by moto get the real creation time, so checks run "later"
LATER = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=8)


def by_id(results):
    return {r["resource"]["resource_id"]: r for r in results}


def put(cw, namespace, name, dims, value, now, hours=168):
    points = [{"MetricName": name, "Value": value, "Timestamp": now - timedelta(hours=h, minutes=30),
               "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]} for h in range(hours)]
    for i in range(0, len(points), 20):
        cw.put_metric_data(Namespace=namespace, MetricData=points[i:i + 20])


@mock_aws
class EbsTests(unittest.TestCase):
    def setUp(self):
        self.ec2 = boto3.client("ec2", region_name=REGION)

    def volume(self, size=100, kind="gp3"):
        return self.ec2.create_volume(AvailabilityZone="us-east-1a", Size=size, VolumeType=kind)["VolumeId"]

    def test_unattached_volume_is_a_finding(self):
        v = self.volume()
        r = by_id(ebs.check_volumes(self.ec2, ACCOUNT, REGION, "scan_1", LATER))[v]
        self.assertEqual(r["outcome"], "finding")
        f = r["findings"][0]
        self.assertEqual(f["sk"], f"FIND#ebs_volume#{REGION}#{v}#ebs_unattached")
        self.assertEqual(f["evidence"]["size_gb"], 100)

    def test_attached_volume_is_clean(self):
        ami = self.ec2.describe_images()["Images"][0]["ImageId"]
        instance = self.ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1,
                                          Placement={"AvailabilityZone": "us-east-1a"})["Instances"][0]["InstanceId"]
        v = self.volume()
        self.ec2.attach_volume(VolumeId=v, InstanceId=instance, Device="/dev/sdf")
        r = by_id(ebs.check_volumes(self.ec2, ACCOUNT, REGION, "scan_1", LATER))[v]
        self.assertEqual(r["outcome"], "clean")
        self.assertEqual(r["resource"]["details"]["attached_to"], [instance])


@mock_aws
class RdsTests(unittest.TestCase):
    def setUp(self):
        self.rds = boto3.client("rds", region_name=REGION)
        self.cw = boto3.client("cloudwatch", region_name=REGION)

    def db(self, name="orders", engine="postgres"):
        self.rds.create_db_instance(DBInstanceIdentifier=name, DBInstanceClass="db.t3.small", Engine=engine,
                                    AllocatedStorage=20, MasterUsername="admin", MasterUserPassword="password123")
        return name

    def check(self):
        return by_id(rds.check_databases(self.rds, self.cw, ACCOUNT, REGION, "scan_1", LATER))

    def test_no_connections_is_a_finding(self):
        db = self.db()
        put(self.cw, "AWS/RDS", "DatabaseConnections", {"DBInstanceIdentifier": db}, 0, LATER)
        r = self.check()[db]
        self.assertEqual(r["outcome"], "finding")
        self.assertEqual(r["findings"][0]["rule"], "rds_idle")
        self.assertEqual(r["findings"][0]["evidence"]["instance_class"], "db.t3.small")

    def test_any_connection_is_clean(self):
        db = self.db()
        put(self.cw, "AWS/RDS", "DatabaseConnections", {"DBInstanceIdentifier": db}, 3, LATER)
        self.assertEqual(self.check()[db]["outcome"], "clean")

    def test_missing_metrics_is_insufficient_data(self):
        db = self.db()
        put(self.cw, "AWS/RDS", "DatabaseConnections", {"DBInstanceIdentifier": db}, 0, LATER, hours=10)
        r = self.check()[db]
        self.assertEqual(r["outcome"], "insufficient_data")
        self.assertEqual(r["findings"], [])


@mock_aws
class LambdaTests(unittest.TestCase):
    def setUp(self):
        self.lam = boto3.client("lambda", region_name=REGION)
        self.cw = boto3.client("cloudwatch", region_name=REGION)
        self.role = boto3.client("iam", region_name=REGION).create_role(
            RoleName="fn", AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}),
        )["Role"]["Arn"]

    def function(self, name):
        code = io.BytesIO()
        with zipfile.ZipFile(code, "w") as z:
            z.writestr("app.py", "def handler(event, context):\n    return 1\n")
        self.lam.create_function(FunctionName=name, Runtime="python3.12", Role=self.role, Handler="app.handler",
                                 Code={"ZipFile": code.getvalue()})
        return name

    def check(self, now=LATER, **kwargs):
        return by_id(functions.check_functions(self.lam, self.cw, ACCOUNT, REGION, "scan_1", now, **kwargs))

    def test_no_invocations_is_a_finding(self):
        fn = self.function("old-report")
        r = self.check()[fn]
        self.assertEqual(r["outcome"], "finding")
        self.assertEqual(r["findings"][0]["evidence"]["invocations"], 0)

    def test_invoked_function_is_clean(self):
        fn = self.function("api")
        put(self.cw, "AWS/Lambda", "Invocations", {"FunctionName": fn}, 5, LATER, hours=2)
        self.assertEqual(self.check()[fn]["outcome"], "clean")

    def test_recently_changed_function_is_skipped(self):
        fn = self.function("new")
        self.assertEqual(self.check(now=datetime.now(timezone.utc))[fn]["outcome"], "ineligible")

    def test_skip_leaves_a_function_out(self):
        self.function("cloud-optimizer-audit")
        self.assertEqual(self.check(skip={"cloud-optimizer-audit"}), {})


@mock_aws
class ElbTests(unittest.TestCase):
    def setUp(self):
        self.ec2 = boto3.client("ec2", region_name=REGION)
        self.elb = boto3.client("elbv2", region_name=REGION)
        self.cw = boto3.client("cloudwatch", region_name=REGION)
        self.vpc = self.ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
        self.subnets = [s["SubnetId"] for s in self.ec2.describe_subnets()["Subnets"]][:2]

    def load_balancer(self, name):
        return self.elb.create_load_balancer(Name=name, Subnets=self.subnets, Type="application")["LoadBalancers"][0]

    def add_target(self, lb):
        group = self.elb.create_target_group(Name=lb["LoadBalancerName"], Protocol="HTTP", Port=80,
                                             VpcId=self.vpc, TargetType="ip")["TargetGroups"][0]
        self.elb.create_listener(LoadBalancerArn=lb["LoadBalancerArn"], Protocol="HTTP", Port=80,
                                 DefaultActions=[{"Type": "forward", "TargetGroupArn": group["TargetGroupArn"]}])
        self.elb.register_targets(TargetGroupArn=group["TargetGroupArn"], Targets=[{"Id": "172.31.0.10"}])

    def check(self):
        return by_id(elb.check_load_balancers(self.elb, self.cw, ACCOUNT, REGION, "scan_1", LATER))

    def test_no_targets_is_a_finding(self):
        self.load_balancer("old-app")
        r = self.check()["old-app"]
        self.assertEqual(r["outcome"], "finding")
        self.assertEqual(r["findings"][0]["rule"], "elb_no_targets")

    def test_no_traffic_is_a_finding(self):
        lb = self.load_balancer("quiet")
        self.add_target(lb)
        r = self.check()["quiet"]
        self.assertEqual(r["findings"][0]["rule"], "elb_idle")
        self.assertEqual(r["findings"][0]["evidence"]["registered_targets"], 1)

    def test_traffic_is_clean(self):
        lb = self.load_balancer("busy")
        self.add_target(lb)
        dim = lb["LoadBalancerArn"].split(":loadbalancer/", 1)[1]
        put(self.cw, "AWS/ApplicationELB", "RequestCount", {"LoadBalancer": dim}, 40, LATER, hours=3)
        self.assertEqual(self.check()["busy"]["outcome"], "clean")


class SavingsTests(unittest.TestCase):
    def finding(self, rule, evidence, region=REGION):
        return {"rule": rule, "region": region, "resource_type": "x", "evidence": evidence}

    def test_rds_price_includes_storage_and_multi_az(self):
        amount, _ = costs.estimate(self.finding("rds_idle", {
            "instance_class": "db.t3.small", "engine": "postgres", "multi_az": True,
            "storage_gb": Decimal("20"), "storage_type": "gp2"}))
        self.assertEqual(amount["amount"], Decimal("51.94"))  # 0.068 x 730 + 20 x 0.115

    def test_unknown_rds_class_has_no_estimate(self):
        amount, note = costs.estimate(self.finding("rds_idle", {"instance_class": "db.x2.huge", "engine": "postgres"}))
        self.assertIsNone(amount)
        self.assertIn("no price", note)

    def test_ebs_price(self):
        amount, _ = costs.estimate(self.finding("ebs_unattached", {"size_gb": Decimal("100"), "volume_type": "gp3"}))
        self.assertEqual(amount["amount"], Decimal("8.00"))

    def test_load_balancer_price(self):
        amount, _ = costs.estimate(self.finding("elb_no_targets", {}))
        self.assertEqual(amount["amount"], Decimal("16.42"))

    def test_lambda_and_other_regions_have_no_estimate(self):
        self.assertIsNone(costs.estimate(self.finding("lambda_unused", {}))[0])
        self.assertIsNone(costs.estimate(self.finding("ebs_unattached", {"size_gb": 1, "volume_type": "gp3"},
                                                      region="eu-west-1"))[0])


if __name__ == "__main__":
    unittest.main()
