from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect

from . import users


def login(request, user):
    request.session.cycle_key()
    request.session["user_id"] = user["user_id"]


def logout(request):
    request.session.flush()


def current_user(request):
    if not hasattr(request, "_user"):
        user = None
        user_id = request.session.get("user_id")
        if user_id:
            user = users.get_user(user_id)
            if not user or user.get("status") != "active":
                request.session.flush()
                user = None
        request._user = user
    return request._user


def login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        request.user_info = current_user(request)
        if not request.user_info:
            if settings.SESSION_COOKIE_NAME in request.COOKIES:
                messages.info(request, "Your session has ended. Sign in again to continue.")
            return redirect("login")
        return view(request, *args, **kwargs)
    return wrapper
