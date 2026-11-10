from functools import wraps

from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.utils.crypto import constant_time_compare, salted_hmac

from . import users


# changes when the password changes, so resetting a password logs out old sessions
def _session_hash(user):
    return salted_hmac("cloud_optimizer.session", user["password_hash"]).hexdigest()


def login(request, user):
    request.session.cycle_key()
    request.session["user_id"] = user["user_id"]
    request.session["auth"] = _session_hash(user)


def logout(request):
    request.session.flush()


def current_user(request):
    if not hasattr(request, "_user"):
        user = None
        user_id = request.session.get("user_id")
        if user_id:
            user = users.get_user(user_id)
            ok = (user and user["status"] == "active"
                  and constant_time_compare(request.session.get("auth", ""), _session_hash(user)))
            if not ok:
                request.session.flush()
                user = None
        request._user = user
    return request._user


def login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        request.user_info = current_user(request)
        if not request.user_info:
            return redirect("login")
        return view(request, *args, **kwargs)
    return wrapper


def admin_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.user_info["role"] != "admin":
            return HttpResponseForbidden("admins only")
        return view(request, *args, **kwargs)
    return wrapper
