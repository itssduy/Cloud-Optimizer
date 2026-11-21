import os
import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import boto3
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client, SimpleTestCase
from moto import mock_aws

from . import oidc, users
from .testutils import sign_in

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@mock_aws
class AuthTests(SimpleTestCase):
    def setUp(self):
        os.environ.update(
            DYNAMODB_TABLE_NAME="test", AWS_REGION="us-east-1",
            COGNITO_DOMAIN="https://login.example.com", COGNITO_USER_POOL_ID="us-east-1_test",
            COGNITO_CLIENT_ID="client123", COGNITO_CLIENT_SECRET="secret",
        )
        os.environ.pop("DYNAMODB_ENDPOINT_URL", None)
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="test",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    def token(self, **changes):
        claims = {"sub": "abc-123", "email": "Duy@Team.com", "token_use": "id", "aud": "client123",
                  "iss": ISSUER, "iat": int(time.time()), "exp": int(time.time()) + 300}
        claims.update(changes)
        return jwt.encode(claims, KEY, algorithm="RS256")

    def sign_in_via_cognito(self, **token_changes):
        start = self.client.get("/auth/start/")
        query = parse_qs(urlparse(start["Location"]).query)
        state, nonce = query["state"][0], query["nonce"][0]
        token = self.token(**{"nonce": nonce, **token_changes})
        with mock.patch.object(oidc, "exchange_code", return_value=token), \
             mock.patch.object(oidc, "signing_key", return_value=KEY.public_key()):
            return self.client.get("/auth/callback/", {"code": "c", "state": state})

    def test_login_page_has_sign_in_button(self):
        self.assertContains(self.client.get("/login/"), "/auth/start/")

    def test_start_redirects_to_cognito(self):
        r = self.client.get("/auth/start/")
        url = urlparse(r["Location"])
        query = parse_qs(url.query)
        self.assertEqual(url.netloc, "login.example.com")
        self.assertEqual(query["client_id"], ["client123"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], ["http://localhost:8000/auth/callback/"])

    def test_full_sign_in(self):
        r = self.sign_in_via_cognito()
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 200)
        user = users.get_user("abc-123")
        self.assertEqual(user["email"], "duy@team.com")

    def test_wrong_state_is_rejected(self):
        self.client.get("/auth/start/")
        r = self.client.get("/auth/callback/", {"code": "c", "state": "forged"})
        self.assertRedirects(r, "/login/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_bad_tokens_are_rejected(self):
        for changes in ({"aud": "someone-else"}, {"iss": "https://evil.example.com"},
                        {"exp": int(time.time()) - 10}, {"token_use": "access"}, {"nonce": "replayed"}):
            client = Client()
            self.client = client
            r = self.sign_in_via_cognito(**changes)
            self.assertRedirects(r, "/login/", fetch_redirect_response=False, msg_prefix=str(changes))
            self.assertEqual(client.get("/").status_code, 302, changes)

    def test_token_signed_by_another_key_is_rejected(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        start = self.client.get("/auth/start/")
        query = parse_qs(urlparse(start["Location"]).query)
        token = jwt.encode({"sub": "x", "email": "x@x.com", "token_use": "id", "aud": "client123", "iss": ISSUER,
                            "iat": int(time.time()), "exp": int(time.time()) + 300, "nonce": query["nonce"][0]},
                           other, algorithm="RS256")
        with mock.patch.object(oidc, "exchange_code", return_value=token), \
             mock.patch.object(oidc, "signing_key", return_value=KEY.public_key()):
            r = self.client.get("/auth/callback/", {"code": "c", "state": query["state"][0]})
        self.assertRedirects(r, "/login/", fetch_redirect_response=False)

    def test_cognito_error_is_rejected(self):
        start = self.client.get("/auth/start/")
        state = parse_qs(urlparse(start["Location"]).query)["state"][0]
        r = self.client.get("/auth/callback/", {"error": "access_denied", "state": state})
        self.assertRedirects(r, "/login/", fetch_redirect_response=False)

    def test_anonymous_is_redirected(self):
        for url in ("/", "/status/", "/ec2/", "/s3/"):
            self.assertRedirects(self.client.get(url), "/login/", fetch_redirect_response=False)

    def test_logout_ends_cognito_session_too(self):
        sign_in(self.client)
        r = self.client.post("/logout/")
        self.assertTrue(r["Location"].startswith("https://login.example.com/logout?"))
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_turned_off_user_is_kicked_out_and_blocked(self):
        self.sign_in_via_cognito()
        users.update_user("abc-123", status="disabled")
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertRedirects(self.sign_in_via_cognito(), "/login/", fetch_redirect_response=False)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        sign_in(client)
        self.assertEqual(client.post("/logout/").status_code, 403)
        self.assertEqual(client.post("/api/ec2/i-0123456789abcdef0/stop/", {"confirm": "yes"}).status_code, 403)
