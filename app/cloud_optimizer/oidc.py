"""Sign in through Cognito's hosted login (and whatever company login is connected to it)."""

import base64
import hashlib
import json
import os
import secrets
import urllib.parse
import urllib.request
from functools import lru_cache

import jwt
from django.conf import settings


class LoginFailed(Exception):
    pass


def config():
    region = os.environ.get("AWS_REGION", "us-east-1")
    pool = os.environ["COGNITO_USER_POOL_ID"]
    return {
        "domain": os.environ["COGNITO_DOMAIN"].rstrip("/"),
        "client_id": os.environ["COGNITO_CLIENT_ID"],
        "client_secret": os.environ["COGNITO_CLIENT_SECRET"],
        "issuer": f"https://cognito-idp.{region}.amazonaws.com/{pool}",
        "redirect_uri": f"{settings.SITE_URL}/auth/callback/",
    }


def start(session):
    """URL of Cognito's login page. Saves state/nonce/PKCE in the session to check on the way back."""
    c = config()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    session["oidc"] = {"state": state, "nonce": nonce, "verifier": verifier}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": c["client_id"],
        "redirect_uri": c["redirect_uri"],
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{c['domain']}/oauth2/authorize?{query}"


def exchange_code(code, verifier):
    c = config()
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": c["client_id"],
        "code": code,
        "redirect_uri": c["redirect_uri"],
        "code_verifier": verifier,
    }).encode()
    basic = base64.b64encode(f"{c['client_id']}:{c['client_secret']}".encode()).decode()
    request = urllib.request.Request(f"{c['domain']}/oauth2/token", data=body, headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)["id_token"]
    except (OSError, KeyError, ValueError) as e:
        raise LoginFailed(f"token exchange failed: {e}")


@lru_cache(maxsize=4)
def jwks_client(issuer):
    return jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json")


def signing_key(token, issuer):
    return jwks_client(issuer).get_signing_key_from_jwt(token).key


def finish(session, params):
    """Checks Cognito's response and returns (sub, email) for the signed-in person."""
    saved = session.pop("oidc", None)
    if not saved or not params.get("state") or not secrets.compare_digest(params["state"], saved["state"]):
        raise LoginFailed("state didn't match")
    if "error" in params or not params.get("code"):
        raise LoginFailed(params.get("error_description") or params.get("error") or "no code")

    c = config()
    token = exchange_code(params["code"], saved["verifier"])
    try:
        claims = jwt.decode(token, signing_key(token, c["issuer"]), algorithms=["RS256"],
                            audience=c["client_id"], issuer=c["issuer"],
                            options={"require": ["exp", "iat", "sub"]})
    except jwt.PyJWTError as e:
        raise LoginFailed(f"bad token: {e}")
    if claims.get("token_use") != "id" or claims.get("nonce") != saved["nonce"]:
        raise LoginFailed("bad token")
    if not claims.get("email"):
        raise LoginFailed("no email on the account")
    return claims["sub"], claims["email"].lower()


def logout_url():
    if "COGNITO_DOMAIN" not in os.environ:
        return None
    c = config()
    query = urllib.parse.urlencode({"client_id": c["client_id"], "logout_uri": f"{settings.SITE_URL}/login/"})
    return f"{c['domain']}/logout?{query}"
