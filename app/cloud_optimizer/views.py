from django.contrib import messages
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import auth, users


def health(request):
    return HttpResponse("ok")


@auth.login_required
def home(request):
    return render(request, "cloud_optimizer/home.html", {"me": request.user_info})


def login_view(request):
    if not users.has_users():
        return redirect("setup")
    if request.method == "POST":
        user = users.authenticate(request.POST.get("email", ""), request.POST.get("password", ""))
        if user:
            auth.login(request, user)
            return redirect("home")
        messages.error(request, "Wrong email or password.")
    return render(request, "cloud_optimizer/login.html")


@require_POST
def logout_view(request):
    auth.logout(request)
    return redirect("login")


def setup(request):
    if users.has_users():
        raise Http404
    if request.method == "POST":
        password = request.POST.get("password", "")
        try:
            validate_password(password)
            user = users.create_user(request.POST["email"], request.POST.get("name", ""), password, role="admin")
        except ValidationError as e:
            for msg in e.messages:
                messages.error(request, msg)
        else:
            auth.login(request, user)
            return redirect("home")
    return render(request, "cloud_optimizer/setup.html")


@auth.admin_required
def users_view(request):
    if request.method == "POST":
        password = request.POST.get("password", "")
        try:
            validate_password(password)
            users.create_user(request.POST["email"], request.POST.get("name", ""), password,
                              role=request.POST.get("role", "member"))
            messages.success(request, f"Added {request.POST['email']}.")
        except ValidationError as e:
            for msg in e.messages:
                messages.error(request, msg)
        except users.EmailTaken:
            messages.error(request, "That email already has an account.")
        return redirect("users")
    return render(request, "cloud_optimizer/users.html",
                  {"me": request.user_info, "users": users.list_users(), "roles": users.ROLES})


@require_POST
@auth.admin_required
def user_update(request, user_id):
    if user_id == request.user_info["user_id"]:
        messages.error(request, "You can't change your own account here.")
        return redirect("users")

    action = request.POST.get("action")
    if action in ("disable", "enable"):
        users.update_user(user_id, status="disabled" if action == "disable" else "active")
    elif action == "role" and request.POST.get("role") in users.ROLES:
        users.update_user(user_id, role=request.POST["role"])
    elif action == "reset_password":
        password = request.POST.get("password", "")
        try:
            validate_password(password)
        except ValidationError as e:
            for msg in e.messages:
                messages.error(request, msg)
            return redirect("users")
        users.set_password(user_id, password)
    else:
        messages.error(request, "Unknown action.")
        return redirect("users")
    messages.success(request, "Saved.")
    return redirect("users")
