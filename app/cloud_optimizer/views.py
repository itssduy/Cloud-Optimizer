import logging

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import auth, dashboard, oidc, users

log = logging.getLogger(__name__)


def health(request):
    return HttpResponse("ok")


@auth.login_required
def home(request):
    return dashboard.overview(request)


def login_view(request):
    if auth.current_user(request):
        return redirect("home")
    return render(request, "cloud_optimizer/login.html")


def login_start(request):
    return redirect(oidc.start(request.session))


def login_callback(request):
    try:
        sub, email = oidc.finish(request.session, request.GET)
    except oidc.LoginFailed as e:
        log.warning("login failed: %s", e)
        messages.error(request, "Sign-in didn't work. Try again.")
        return redirect("login")
    user = users.signed_in(sub, email)
    if user.get("status") != "active":
        messages.error(request, "Your access to Cloud Optimizer has been turned off.")
        return redirect("login")
    auth.login(request, user)
    return redirect("home")


@require_POST
def logout_view(request):
    auth.logout(request)
    # also end the Cognito session, otherwise "Sign in" logs straight back in
    return redirect(oidc.logout_url() or "login")
