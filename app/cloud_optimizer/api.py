from functools import wraps

from botocore.exceptions import BotoCoreError, ClientError
from django.http import JsonResponse

from . import auth, data


def api(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method != "GET":
            return JsonResponse({"error": "method_not_allowed"}, status=405)
        try:
            request.user_info = auth.current_user(request)
            if not request.user_info:
                return JsonResponse({"error": "not_logged_in"}, status=401)
            return JsonResponse(view(request, *args, **kwargs), safe=False)
        except data.NotFound:
            return JsonResponse({"error": "not_found"}, status=404)
        except (BotoCoreError, ClientError):
            return JsonResponse({"error": "store_unavailable"}, status=503)
    return wrapper


@api
def status(request):
    return data.status()


@api
def scans(request):
    return {"scans": data.scans()}


@api
def resources(request):
    return {"resources": data.resources(request.GET.get("type"))}


@api
def resource(request, resource_type, resource_id):
    return data.resource(resource_type, resource_id)


@api
def findings(request):
    status = request.GET.get("status", "open")
    return {"findings": data.findings(None if status == "all" else status)}


@api
def costs(request):
    return {"costs": data.costs(), "savings": data.savings()}


@api
def alerts(request):
    return {"alerts": data.alerts(request.GET.get("status"))}


@api
def actions(request):
    return {"actions": data.actions()}
