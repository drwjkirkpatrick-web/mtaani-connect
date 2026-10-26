"""Upload endpoint (Prompt 06). Authenticated POST, one image at a time."""
import json

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from apps.core import images
from .services import ALLOWED_PURPOSES, save_upload


@require_http_methods(["POST"])
def upload_view(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login required / ingia kwanza"}, status=403)
    purpose = request.POST.get("purpose", "post")
    if purpose not in ALLOWED_PURPOSES:
        return JsonResponse({"error": "bad purpose / nia batili"}, status=400)
    upload = request.FILES.get("image")
    if upload is None:
        return JsonResponse(
            {"error": "attach an image under field 'image' / weka picha kwenye 'image'"},
            status=400,
        )
    try:
        ref = save_upload(file_obj=upload, owner=request.user, purpose=purpose)
    except images.TooLarge as exc:
        return JsonResponse({"error": str(exc)}, status=413)
    except images.InvalidImage as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(
        {
            "uuid": str(ref.id),
            "purpose": ref.purpose,
            "standard": {"path": ref.standard_path, "bytes": ref.standard_bytes,
                          "w": ref.standard_w, "h": ref.standard_h},
            "thumb": {"path": ref.thumb_path, "bytes": ref.thumb_bytes,
                      "w": ref.thumb_w, "h": ref.thumb_h},
        },
        status=201,
    )
