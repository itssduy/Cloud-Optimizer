import os

import boto3
from django.test import SimpleTestCase
from moto import mock_aws

from . import users


@mock_aws
class AuthTests(SimpleTestCase):
    def setUp(self):
        os.environ["DYNAMODB_TABLE_NAME"] = "test"
        os.environ["AWS_REGION"] = "us-east-1"
        os.environ.pop("DYNAMODB_ENDPOINT_URL", None)
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="test",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    def make_admin(self):
        return users.create_user("admin@team.com", "Admin", "correct-horse-9", role="admin")

    def log_in(self, email, password):
        return self.client.post("/login/", {"email": email, "password": password})

    def test_first_visit_goes_to_setup(self):
        self.assertRedirects(self.client.get("/login/"), "/setup/", fetch_redirect_response=False)

    def test_setup_creates_admin_then_closes(self):
        r = self.client.post("/setup/", {"email": "Admin@Team.com", "name": "A", "password": "correct-horse-9"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertEqual(users.list_users()[0]["role"], "admin")
        self.assertEqual(users.list_users()[0]["email"], "admin@team.com")
        self.assertEqual(self.client.get("/setup/").status_code, 404)

    def test_setup_rejects_weak_password(self):
        self.client.post("/setup/", {"email": "a@team.com", "password": "123"})
        self.assertFalse(users.has_users())

    def test_anonymous_is_redirected(self):
        self.make_admin()
        for url in ("/", "/users/"):
            self.assertRedirects(self.client.get(url), "/login/", fetch_redirect_response=False)

    def test_login_and_logout(self):
        self.make_admin()
        self.assertRedirects(self.log_in("admin@team.com", "correct-horse-9"), "/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.post("/logout/")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_wrong_password(self):
        self.make_admin()
        r = self.log_in("admin@team.com", "nope")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Wrong email or password")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_member_cannot_manage_users(self):
        self.make_admin()
        member = users.create_user("m@team.com", "M", "correct-horse-9")
        self.log_in("m@team.com", "correct-horse-9")
        self.assertEqual(self.client.get("/users/").status_code, 403)
        self.assertEqual(self.client.post(f"/users/{member['user_id']}/", {"action": "disable"}).status_code, 403)

    def test_admin_adds_user(self):
        self.make_admin()
        self.log_in("admin@team.com", "correct-horse-9")
        self.client.post("/users/", {"email": "new@team.com", "password": "another-pass-7", "role": "member"})
        self.assertIn("new@team.com", [u["email"] for u in users.list_users()])
        self.client.post("/users/", {"email": "new@team.com", "password": "another-pass-7"})
        self.assertEqual(len(users.list_users()), 2)

    def test_disabled_user_is_kicked_out(self):
        self.make_admin()
        member = users.create_user("m@team.com", "M", "correct-horse-9")
        self.log_in("m@team.com", "correct-horse-9")
        users.update_user(member["user_id"], status="disabled")
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.log_in("m@team.com", "correct-horse-9").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_password_reset_ends_old_sessions(self):
        self.make_admin()
        member = users.create_user("m@team.com", "M", "correct-horse-9")
        self.log_in("m@team.com", "correct-horse-9")
        users.set_password(member["user_id"], "brand-new-pass-3")
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertIsNotNone(users.authenticate("m@team.com", "brand-new-pass-3"))

    def test_admin_cannot_disable_self(self):
        admin = self.make_admin()
        self.log_in("admin@team.com", "correct-horse-9")
        self.client.post(f"/users/{admin['user_id']}/", {"action": "disable"})
        self.assertEqual(users.get_user(admin["user_id"])["status"], "active")
