"""Messaging views (Prompt 20). Everything member-facing is login_required —
a chat is private by definition; the app's only public render is the privacy
note.

IDOR CONTRACT (PROMPTS.md pitfall): every thread-scoped route resolves the
thread through services.get_thread_for → Thread.objects.for_participant and
answers 404 (NOT 403) for non-members — a distinguishable error would confirm
to a guessed-ID attacker that the thread exists. Anonymous users get the
ordinary login redirect, like every other login_required page.

Privacy: contact details are never auto-shared; templates render usernames,
message bodies via |linebreaks (never |safe) and photo variants only.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import F, Max
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core import images  # InvalidImage surfaces as a friendly notice

from . import services
from .forms import MessageForm, ReportMessageForm
from .models import MAX_IMAGES, Thread

PAGE_SIZE = 20  # threads per chats-list page (hard rule: 50 KB mobile budget)
MESSAGE_WINDOW = services.NEWEST_MESSAGE_WINDOW


def thread_list(request):
    """The viewer's conversations (route name exactly 'list' — parent-owned
    base.html tab points here).

    Anonymous visitors get an honest sign-in prompt (tab-route contract:
    every tab renders 200 like /guides/ — NOT a redirect), while chat DATA
    stays login-only. ?show=muted reveals threads the member muted ON
    THEMSELVES (with an unmute button each); default hides them.

    ?listing=<pk> renders the marketplace-enquiry panel: the listings app
    deep-links here ('/chats/?listing=<pk>'); the panel POSTs the real
    start (POST-only state change, CSRF-checked).
    """
    if not getattr(request.user, "is_authenticated", False):
        return render(
            request,
            "messaging/list_anonymous.html",
            {"login_url": settings.LOGIN_URL},
        )
    muted_only = request.GET.get("show", "") == "muted"
    # ONE filter() call: both conditions must land on the SAME through-row
    # (the viewer's own membership). Splitting across two filter() calls
    # joins the M2M twice and lets ANY participant's mute flag hide the
    # thread for everyone — the join-reuse rule is subtle, so spell it out.
    threads = (
        Thread.objects.filter(
            memberships__user=request.user,
            memberships__muted=muted_only,
        )
        .annotate(last_activity=Max("messages__created_at"))
        .order_by(F("last_activity").desc(nulls_last=True))
    )
    page_obj = Paginator(threads, PAGE_SIZE).get_page(request.GET.get("page"))
    page_threads = list(page_obj.object_list)
    unreads = services.unread_counts(page_threads, request.user)
    for t in page_threads:  # view-attached display counts (badges on the page)
        t.unread_n = unreads.get(t.pk, 0)

    enquiry = None
    listing_ref = request.GET.get("listing", "").strip()
    if listing_ref:
        listing = services.resolve_public_listing(listing_ref)
        enquiry = {
            "id": listing_ref,  # rendered verbatim as 'Listing #<ref>'
            "title": (listing[1] if listing else None) or f"Listing #{listing_ref}",
        }
    return render(
        request,
        "messaging/list.html",
        {
            "threads": page_threads,
            "page_obj": page_obj,
            "unreads": unreads,
            "muted_only": muted_only,
            "enquiry": enquiry,
        },
    )


def _report_choices(thread, viewer) -> list[tuple[str, str]]:
    """Choices for the report form: THIS thread's currently-rendered message
    window. Shared by the detail render and the report route so the picker
    offered is EXACTLY what the route validates (never a stray message_pk)."""
    page = services.messages_window(thread)
    other = services.counterpart_of(thread, viewer)
    other_name = other.user.username if other else _("(member)")
    return [
        (
            str(m.pk),
            _("%(time)s — %(name)s: %(text)s")
            % {
                "time": timezone.localtime(m.created_at).strftime("%H:%M"),
                "name": _("You") if m.sender_id == viewer.pk else other_name,
                # 40-char slice keeps the picker readable; '(photo)' when the
                # send was a photo without body text.
                "text": m.body[:40] or _("(photo)"),
            },
        )
        for m in page
    ]


@login_required
def thread_detail(request, pk):
    """Thread view + send form (route 'detail'). GET marks the member's read
    watermark; the 'Seen' badge rendering is a separate, opt-out-able display
    decision (services.seen_flags). POST sends a message through the service
    (membership + caps enforced there; atomic so no half-written sends)."""
    thread = services.get_thread_for(request.user, pk)
    if thread is None:
        raise Http404("No such conversation.")
    me = services.participant_of(thread, request.user)

    if request.method == "POST":
        form = MessageForm(request.POST)
        uploads = [f for f in request.FILES.getlist("images") if f]
        if form.is_valid():
            try:
                services.send_message(
                    thread=thread,
                    sender=request.user,
                    body=form.cleaned_data.get("body", ""),
                    uploads=uploads,
                )
            except images.InvalidImage as exc:
                messages.error(request, str(exc))
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                return redirect("messaging:detail", pk=thread.pk)  # PRG
    else:
        form = MessageForm()

    # Visiting marks read (always — unread correctness); whether the OTHER
    # member's watermark is DISPLAYED as 'Seen' honors both opt-outs.
    services.mark_read(me)
    page = services.messages_window(thread, limit=MESSAGE_WINDOW)
    other = services.counterpart_of(thread, request.user)
    seen = services.seen_flags(me, other, page)
    return render(
        request,
        "messaging/detail.html",
        {
            "thread": thread,
            # honest label until the parent FKs the reference (module docstring)
            "origin_reference": (
                thread.origin_listing_id
                if thread.origin_kind == "listing"
                else None
            ),
            "me": me,
            "other": other.user if other else None,
            "message_list": page,
            "seen_ids": [pk for pk, is_seen in seen.items() if is_seen],
            "form": form,
            "report_form": ReportMessageForm(
                messages_choices=_report_choices(thread, request.user)
            ),
            "max_images": MAX_IMAGES,
        },
    )


@login_required
@require_POST
def start_for_listing(request):
    """POST buyer→seller enquiry origin (route 'start_for_listing'). The
    listing reference comes from the POST field (or ?listing= for both
    shapes of the listings deep-link). POST-only: creating a thread is a
    state change, GET never mutates."""
    listing_ref = request.POST.get("listing") or request.GET.get("listing")
    try:
        thread = services.create_enquiry_thread(
            initiator=request.user, listing_id=listing_ref
        )
    except services.ListingUnavailable as exc:
        # PROMPTS.md "no fabricated data": unresolvable listing → honest page,
        # NO thread row, NO invented seller. This is also what renders while
        # the sibling listings app/its public API is still landing.
        return render(
            request,
            "messaging/listing_unavailable.html",
            {"notice": exc.message},
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("messaging:list")
    return redirect("messaging:detail", pk=thread.pk)


@login_required
@require_POST
def thread_mute(request, pk):
    """Self-mute from the thread (keeps the member's full history; hides the
    thread from their own chats list until unmuted)."""
    thread = services.get_thread_for(request.user, pk)
    if thread is None:
        raise Http404("No such conversation.")
    services.set_muted(services.participant_of(thread, request.user), True)
    messages.success(
        request,
        _(
            "Conversation muted — it's hidden from your chats list (your "
            "history stays). You can unmute it from the muted list."
        ),
    )
    return redirect("messaging:list")


@login_required
@require_POST
def thread_unmute(request, pk):
    """Unmute returns the thread to the default chats list."""
    thread = services.get_thread_for(request.user, pk)
    if thread is None:
        raise Http404("No such conversation.")
    services.set_muted(services.participant_of(thread, request.user), False)
    messages.success(request, _("Conversation unmuted."))
    return redirect("messaging:detail", pk=thread.pk)


@login_required
@require_POST
def receipt_settings(request, pk):
    """Per-thread read-receipts opt in/out (route 'receipts', default ON).

    NOTE: hiding receipts is a DISPLAY choice; the read watermark still
    advances so unread counts stay honest for the other member.
    """
    thread = services.get_thread_for(request.user, pk)
    if thread is None:
        raise Http404("No such conversation.")
    me = services.participant_of(thread, request.user)
    show = request.POST.get("opt_in", "") == "true"
    services.set_receipts(me, show)
    messages.success(
        request,
        _("Read receipts updated — %(state)s.")
        % {"state": _("showing") if show else _("hidden")},
    )
    return redirect("messaging:detail", pk=thread.pk)


@login_required
@require_POST
def report_message(request, pk):
    """Report one of the thread's messages into the Prompt 15 queue.

    Membership first (route gate → plain 404 for non-members); then the
    message must belong to THIS thread (thread.messages scope — a second
    gate on the generic target). Rate-limited reports are caught
    GRACEFULLY: the localized notice shows, the message stays sent, the
    thread stays usable — no 5xx, no thread mutation on failure.
    """
    thread = services.get_thread_for(request.user, pk)
    if thread is None:
        raise Http404("No such conversation.")
    form = ReportMessageForm(
        request.POST, messages_choices=_report_choices(thread, request.user)
    )
    if not form.is_valid():
        messages.error(request, _("Pick a message and a reason to report."))
        return redirect("messaging:detail", pk=thread.pk)
    message = (
        thread.messages.filter(pk=form.cleaned_data["message_pk"]).first()
    )
    if message is None:
        raise Http404("No such message in this conversation.")
    try:
        services.report_message(
            reporter=request.user,
            message=message,
            reason=form.cleaned_data["reason"],
            note=form.cleaned_data.get("note", ""),
        )
    except ValidationError as exc:
        if getattr(exc, "code", None) == "report_rate_limited":
            messages.warning(request, "; ".join(exc.messages))
            return redirect("messaging:detail", pk=thread.pk)
        messages.error(request, "; ".join(exc.messages))
        return redirect("messaging:detail", pk=thread.pk)
    messages.success(
        request,
        _(
            "Report sent to the moderation team. This conversation is now "
            "muted for you — unmute it from your chats list if you change "
            "your mind."
        ),
    )
    return redirect("messaging:list")


def privacy_note(request):
    """The only public render in the app: the honest V1 privacy text (TLS in
    transit, server-stored, NO end-to-end encryption — plain words, no
    overselling). Linked from chat pages so it's reachable before signup."""
    return render(request, "messaging/privacy.html")
