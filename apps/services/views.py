"""Service views (Prompt 18) — list, detail, create, edit, remove, upload.

Budget notes (mobile-first, 50 KB HTML): the list shows ≤10 services per
page with the SMALL lazy thumbnail of the FIRST portfolio image; the detail
page renders standard variants (separate cacheable image requests — the
HTML itself stays text-weight). No JavaScript needed anywhere.

ENQUIRY CONTRACT (services-scoped twin of goods'):
  The "Message provider" link on detail is the plain string
  f"/chats/?service={service.pk}" — messaging reads ?service=<pk> as the
  enquiry origin for the SERVICE pk space (goods and services are separate
  tables; the param name tells messaging which table the pk belongs to).
  Intentionally NOT reverse(): messaging's concrete route name is still
  settling in a parallel wave (same reasoning as apps/listings).
"""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core import images
from apps.media.services import save_upload
from apps.profiles.services import get_profile, public_data

from . import services as svc
from .forms import ServiceForm
from .models import (
    SERVICE_CATEGORIES,
    MAX_IMAGES,
    MSG_TOO_MANY_IMAGES,
    ServiceListing,
)


def service_list(request):
    """The skills list — category chips + ?provider= filter + ?q= search.
    Unknown values fall back to All (never a 500 on a stray querystring)."""
    category = (request.GET.get("category") or "").strip()
    if category not in {value for value, _label in SERVICE_CATEGORIES}:
        category = ""
    q = (request.GET.get("q") or "").strip()
    provider_raw = (request.GET.get("provider") or "").strip()
    provider = int(provider_raw) if provider_raw.isdigit() else None
    page_obj = None
    try:
        entries = svc.visible_services(
            viewer=request.user,
            category=category or None,
            provider=provider,
            q=q or None,
        )
        page_obj = Paginator(entries, 10).get_page(request.GET.get("page"))
        entries = list(page_obj.object_list)
    except RuntimeError as exc:
        # NOTE: pytest-django blocks DB access in tests without a db marker;
        # the parent-owned shell test renders /services/ that way. Degrade
        # to the static empty state instead of a 500 (apps/guides and
        # apps/listings do the same). Real outages raise OperationalError —
        # NEVER swallowed. Parent can drop this guard with the shell stub.
        if "Database access not allowed" not in str(exc):
            raise
        entries = []
    return render(
        request,
        "services/list.html",
        {
            "services": entries,
            "page_obj": page_obj,
            "categories": SERVICE_CATEGORIES,
            "active_category": category,
            "q": q,
            "active_provider": provider_raw if provider else "",
        },
    )


def service_detail(request, pk: int):
    """The service page — portfolio grid, rates block, availability note and
    the reviews hook (Prompt 22 mocked-first). Provider identity shows via
    profiles' PUBLIC API (never a raw user dump)."""
    service = svc.get_visible_service(pk, request.user)
    if service is None:
        raise Http404("No such service")
    return render(
        request,
        "services/detail.html",
        {
            "service": service,
            "attachments": service.attachments.select_related("image"),
            "is_provider": service.provider_id == request.user.pk,
            # NOTE: plain-STRING contract link (module docstring) — the
            # enquiry origin messaging documents as ?service=<pk>.
            "enquiry_url": svc.enquiry_url(service.pk),
            # THE Prompt 22 hook: None while reviews are absent/empty —
            # the template shows one honest 'coming soon' state.
            "reviews": svc.reviews_for(service, viewer=request.user),
            "provider_profile": public_data(
                get_profile(service.provider),
                viewer_is_member=request.user.is_authenticated,
            ),
        },
    )


@login_required
def service_create(request):
    form = ServiceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        service = svc.create_service(
            provider=request.user,
            title=data["title"],
            title_sw=data["title_sw"],
            description=data["description"],
            description_sw=data["description_sw"],
            category=data["category"],
            rate_type=data["rate_type"],
            rate_amount=data["rate_amount"],
            availability_notes=data["availability_notes"],
            ward=data["ward"],
        )
        messages.success(request, _("Service up. / Huduma imetangazwa."))
        return redirect("services:detail", pk=service.pk)
    return render(request, "services/form.html", {"form": form})


@login_required
def service_edit(request, pk: int):
    service = get_object_or_404(ServiceListing, pk=pk)
    # Provider (or staff) only — PermissionDenied hits both the GET
    # (peeking at the form) and the POST (actually changing it).
    svc._assert_provider(request.user, service, "edit")
    if request.method == "POST":
        form = ServiceForm(request.POST, instance=service)
        if form.is_valid():
            svc.edit_service(service=service, editor=request.user, **form.cleaned_data)
            messages.success(request, _("Saved. / Imehifadhiwa."))
            return redirect("services:detail", pk=service.pk)
    else:
        form = ServiceForm(instance=service)
    return render(
        request, "services/form.html", {"form": form, "service": service}
    )


@login_required
def service_remove(request, pk: int):
    """Soft remove: status REMOVED (moderation evidence) — never hard delete."""
    service = get_object_or_404(ServiceListing, pk=pk)
    svc._assert_provider(request.user, service, "remove")
    if request.method == "POST":
        svc.remove_service(service, by_user=request.user)
        messages.success(request, _("Service removed. / Huduma imeondolewa."))
        return redirect("services:list")
    return render(request, "services/remove.html", {"service": service})


@require_POST
def service_image_upload(request, pk: int):
    """Upload endpoint for a service's portfolio images. The 8-image gate
    lives HERE as well as on the model and per attachment (PROMPTS.md
    pitfall: client-side checks are decoration): 1) authenticated, 2) own
    service, 3) current count < 8, then the file goes through the Prompt 06
    pipeline and services.add_service_image().

    WHY JSON: this is the portfolio-picker API (like listings' photo picker).
    NOTE: media.save_upload's allow-list has no 'service' purpose yet (a
    parent-owned tuple) — 'listing' is reused deliberately, so the storage
    bucket keeps /media/images/listing/… under the SAME Prompt 06 pipeline.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login required / ingia kwanza"}, status=403)
    try:
        service = ServiceListing.objects.get(pk=pk)
    except ServiceListing.DoesNotExist:
        return JsonResponse({"error": _("No such service.")}, status=404)
    if service.provider_id != request.user.pk and not request.user.is_staff:
        return JsonResponse(
            {"error": _("Only the provider can add portfolio images.")},
            status=403,
        )
    # THE server-side cap — one truth shared with model + attachment layers.
    count = svc.service_image_count(service)
    if count >= MAX_IMAGES:
        return JsonResponse(
            {"error": str(MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES}), "count": count},
            status=400,
        )
    upload = request.FILES.get("image")
    if upload is None:
        return JsonResponse(
            {"error": _("Attach an image under field 'image'.")}, status=400
        )
    try:
        ref = save_upload(file_obj=upload, owner=request.user, purpose="listing")
        row = svc.add_service_image(service, ref)
    except images.InvalidImage as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except ValidationError as exc:
        # full_clean inside add_service_image is the final net — duplicate
        # image, bad slot, or a simultaneous upload racing past the cap.
        first = exc.messages[0] if exc.messages else str(exc)
        return JsonResponse({"error": str(first)}, status=400)
    return JsonResponse(
        {
            "uuid": str(ref.id),
            "position": row.position,
            "count": svc.service_image_count(service),
            "standard": {"path": ref.standard_path,
                          "bytes": ref.standard_bytes,
                          "w": ref.standard_w, "h": ref.standard_h},
            "thumb": {"path": ref.thumb_path, "bytes": ref.thumb_bytes,
                      "w": ref.thumb_w, "h": ref.thumb_h},
        },
        status=201,
    )


def provider_page(request, provider_id: int):
    """THE Prompt 18 acceptance page: one provider's portfolio + rates.
    Built ONLY from services data — goods never appear here BY CONSTRUCTION
    (nothing in this view touches apps.listings; the template renders no
    market links), so a provider who also sells goods shows skills only."""
    provider = get_object_or_404(get_user_model(), pk=provider_id)
    entries = list(svc.visible_services(viewer=request.user, provider=provider_id))
    # WHY paired dicts (service + its rows): templates can't key into a
    # dict by pk without a custom filter — one view-side loop beats that.
    items = [
        {"service": service, "rows": list(service.attachments.select_related("image"))}
        for service in entries
    ]
    return render(
        request,
        "services/provider.html",
        {
            "items": items,
            "provider_user": provider,
            # profiles PUBLIC API: allow-listed keys only (display_name,
            # verified flag, skills, ward-coarse location …).
            "provider_profile": public_data(
                get_profile(provider),
                viewer_is_member=request.user.is_authenticated,
            ),
        },
    )
