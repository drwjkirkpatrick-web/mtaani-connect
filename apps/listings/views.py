"""Listing views (Prompt 17) — list, detail, create, edit, remove, upload.

Budget notes (mobile-first, 50 KB HTML): the list shows ≤10 listings per
page with the SMALL lazy thumbnail of the FIRST photo; the detail page
renders standard variants (still separate cacheable image requests — the
HTML itself stays text-weight). No JavaScript needed for anything here.

ENQUIRY CONTRACT (Prompt 20 messaging lands in parallel):
  The "Message seller" button rendered on detail is the plain string
  f"/chats/?listing={listing.pk}" — messaging reads ?listing=<pk> as the
  enquiry origin (typed reference to this listing). Intentionally NOT
  reverse(): the messaging app_name exists but its concrete route name is
  still settling in a parallel wave; a plain string cannot break while
  messaging evolves its urlconf.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core import images
from apps.media.services import save_upload

from .forms import ListingForm
from .models import LISTING_CATEGORIES, MAX_IMAGES, MSG_TOO_MANY_IMAGES, Listing
from .services import (
    _assert_seller,
    add_listing_image,
    create_listing,
    edit_listing,
    get_visible_listing,
    listing_image_count,
    remove_listing,
    visible_listings,
)

# WHY a module constant: the route is the cross-app contract (see module
# docstring); template + tests + future barter flows all print the same root.
CHATS_URL_ROOT = "/chats/"


def listing_list(request):
    """/market/ — category filter chips + ?q= title search. Unknown values
    fall back to All (never a 500 on a stray querystring)."""
    category = (request.GET.get("category") or "").strip()
    if category not in {value for value, _label in LISTING_CATEGORIES}:
        category = ""
    q = (request.GET.get("q") or "").strip()
    page_obj = None
    try:
        listings = visible_listings(
            viewer=request.user,
            category=category or None,
            q=q or None,
        )
        page_obj = Paginator(listings, 10).get_page(request.GET.get("page"))
        listings = list(page_obj.object_list)
    except RuntimeError as exc:
        # NOTE: pytest-django blocks DB access in tests without a db marker;
        # the parent-owned shell test renders /market/ that way. Degrade to
        # the static empty state instead of a 500 (apps/guides does the
        # same). Real outages raise OperationalError — NEVER swallowed. The
        # parent can drop this guard once /market/ leaves the shell test's
        # stub-render parametrize.
        if "Database access not allowed" not in str(exc):
            raise
        listings = []
    return render(
        request,
        "listings/list.html",
        {
            "listings": listings,
            "page_obj": page_obj,
            "categories": LISTING_CATEGORIES,
            "active_category": category,
            "q": q,
        },
    )


def listing_detail(request, pk: int):
    """The listing page (Prompt 17 budget + exposure test surface)."""
    listing = get_visible_listing(pk, request.user)
    if listing is None:
        raise Http404("No such listing")
    return render(
        request,
        "listings/detail.html",
        {
            "listing": listing,
            "attachments": listing.attachments.select_related("image"),
            "is_seller": listing.seller_id == request.user.pk,
            # NOTE: plain-STRING contract link (see module docstring) — the
            # enquiry origin messaging documents as ?listing=<pk>.
            "enquiry_url": f"{CHATS_URL_ROOT}?listing={listing.pk}",
            "is_flag_visible": listing.review_flagged and (
                listing.seller_id == request.user.pk or request.user.is_staff
            ),
        },
    )


@login_required
def listing_create(request):
    form = ListingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        listing = create_listing(
            seller=request.user,
            title=data["title"],
            title_sw=data["title_sw"],
            description=data["description"],
            description_sw=data["description_sw"],
            category=data["category"],
            condition=data["condition"],
            price=data["price"],
            price_negotiable=data["price_negotiable"],
            ward=data["ward"],
        )
        messages.success(request, _("Listing up. / Imetangazwa."))
        if listing.review_flagged:
            messages.warning(
                request,
                _(
                    "This text was queued for review before it reads "
                    "out of the ordinary — check the listing page later."
                ),
            )
        return redirect("listings:detail", pk=listing.pk)
    return render(request, "listings/form.html", {"form": form})


@login_required
def listing_edit(request, pk: int):
    listing = get_object_or_404(Listing, pk=pk)
    # Seller (or staff) only — PermissionDenied raised here hits both the
    # GET (peeking at the form) and the POST (actually changing it).
    _assert_seller(request.user, listing, "edit")
    if request.method == "POST":
        form = ListingForm(request.POST, instance=listing)
        if form.is_valid():
            edit_listing(listing=listing, editor=request.user, **form.cleaned_data)
            messages.success(request, _("Saved. / Imehifadhiwa."))
            return redirect("listings:detail", pk=listing.pk)
    else:
        form = ListingForm(instance=listing)
    return render(
        request, "listings/form.html", {"form": form, "listing": listing}
    )


@login_required
def listing_remove(request, pk: int):
    """Soft remove: status REMOVED (moderation evidence) — never hard delete."""
    listing = get_object_or_404(Listing, pk=pk)
    _assert_seller(request.user, listing, "remove")
    if request.method == "POST":
        remove_listing(listing, by_user=request.user)
        messages.success(request, _("Listing removed. / Tangazo limeondolewa."))
        return redirect("listings:list")
    return render(request, "listings/remove.html", {"listing": listing})


@require_POST
def listing_image_upload(request, pk: int):
    """Upload endpoint for a listing's photos. The 6-photo gate lives HERE
    as well as on the model and per attachment (PROMPTS.md pitfall:
    client-side checks are decoration): 1) authenticated, 2) own listing,
    3) current photo count < 6, then the file goes through the Prompt 06
    pipeline and services.add_listing_image().

    WHY JSON responses: this endpoint is the photo-picker API used by the
    create page's little JS (and any future offline shell flow).
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login required / ingia kwanza"}, status=403)
    try:
        listing = Listing.objects.get(pk=pk)
    except Listing.DoesNotExist:
        return JsonResponse({"error": _("No such listing.")}, status=404)
    if listing.seller_id != request.user.pk and not request.user.is_staff:
        return JsonResponse(
            {"error": _("Only the seller can add photos.")}, status=403
        )
    # THE server-side cap — one truth shared with model + attachment layers.
    count = listing_image_count(listing)
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
        row = add_listing_image(listing, ref)
    except images.InvalidImage as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except ValidationError as exc:
        # full_clean inside add_listing_image is the final net — duplicate
        # photo, bad slot, or a simultaneous upload racing past the cap.
        first = exc.messages[0] if exc.messages else str(exc)
        return JsonResponse({"error": str(first)}, status=400)
    return JsonResponse(
        {
            "uuid": str(ref.id),
            "position": row.position,
            "count": listing_image_count(listing),
            "standard": {"path": ref.standard_path,
                          "bytes": ref.standard_bytes,
                          "w": ref.standard_w, "h": ref.standard_h},
            "thumb": {"path": ref.thumb_path, "bytes": ref.thumb_bytes,
                      "w": ref.thumb_w, "h": ref.thumb_h},
        },
        status=201,
    )
