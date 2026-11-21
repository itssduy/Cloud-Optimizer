from django.conf import settings

from . import users


def sign_in(client, email="m@team.com"):
    """Signs a test client in without going through Cognito."""
    user = users.signed_in(f"sub-{email}", email)
    session = client.session
    session["user_id"] = user["user_id"]
    session.save()
    client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
    return user
