import os
import uuid
from unittest import mock

import boto3
from django.test import SimpleTestCase
from moto import mock_aws

from . import actions, users

ACCOUNT = "123456789012"


@mock_aws
class Ec2ActionTests(SimpleTestCase):
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
        self.ec2 = boto3.client("ec2", region_name="us-east-1")
        ami = self.ec2.describe_images()["Images"][0]["ImageId"]
        self.instance = self.ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
        self.user = users.create_user("m@team.com", "M", "correct-horse-9")
        self.client.post("/login/", {"email": "m@team.com", "password": "correct-horse-9"})

    def act(self, action="stop", instance=None, confirm="yes", request_id=None):
        return self.client.post(f"/api/ec2/{instance or self.instance}/{action}/",
                                {"confirm": confirm, "request_id": request_id or str(uuid.uuid4())})

    def state(self):
        return self.ec2.describe_instances(InstanceIds=[self.instance])["Reservations"][0]["Instances"][0]["State"]["Name"]

    def test_stop_then_verify(self):
        r = self.act("stop", request_id="req-00000001")
        self.assertEqual(r.status_code, 202)
        action = r.json()["action"]
        self.assertEqual(action["requested_by"], self.user["user_id"])
        self.assertEqual(action["change"], {"before": {"state": "running"}, "after": {"state": "stopped"}})
        self.assertEqual(action["outcome"], "unknown")
        self.assertEqual(self.state(), "stopped")

        checked = self.client.get("/api/actions/req-00000001/").json()["action"]
        self.assertEqual(checked["outcome"], "verified")
        self.assertEqual(checked["observed_state"], "stopped")

    def test_start_stopped_instance(self):
        self.ec2.stop_instances(InstanceIds=[self.instance])
        self.assertEqual(self.act("start").status_code, 202)
        self.assertEqual(self.state(), "running")

    def test_not_confirmed_does_nothing(self):
        self.assertEqual(self.act(confirm="no").status_code, 400)
        self.assertEqual(self.state(), "running")

    def test_repeated_request_does_nothing(self):
        self.assertEqual(self.act(request_id="req-00000002").status_code, 202)
        self.ec2.start_instances(InstanceIds=[self.instance])
        self.assertEqual(self.act(request_id="req-00000002").status_code, 409)
        self.assertEqual(self.state(), "running")

    def test_wrong_state(self):
        r = self.act("start")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["state"], "running")

    def test_unknown_or_tampered_instance(self):
        self.assertEqual(self.act(instance="i-0123456789abcdef0").status_code, 404)
        self.assertEqual(self.act(instance="not-an-id").status_code, 404)
        self.assertEqual(self.act(action="terminate").status_code, 404)

    def test_other_account_instance_is_not_found(self):
        with mock.patch.dict(os.environ, AWS_ACCOUNT_ID="999999999999"):
            self.assertEqual(self.act().status_code, 404)
        self.assertEqual(self.state(), "running")

    def test_asg_instance_is_blocked(self):
        with mock.patch.object(actions, "blocked_reason", return_value="managed by an auto scaling group"):
            r = self.act()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.state(), "running")

    def test_aws_error_is_recorded(self):
        from botocore.exceptions import ClientError
        boom = ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "no"}}, "StopInstances")
        real = actions.ec2

        def fake():
            client = real()
            client.stop_instances = mock.Mock(side_effect=boom)
            return client
        with mock.patch.object(actions, "ec2", fake):
            r = self.act(request_id="req-00000003")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["action"]["request_error"], "UnauthorizedOperation")
        self.assertEqual(r.json()["action"]["outcome"], "failed")

    def test_logged_out_and_disabled(self):
        users.update_user(self.user["user_id"], status="disabled")
        self.assertEqual(self.act().status_code, 401)
        self.client.post("/logout/")
        self.assertEqual(self.act().status_code, 401)
        self.assertEqual(self.state(), "running")
