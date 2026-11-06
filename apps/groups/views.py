"""Group views (PROMPTS.md Prompt 13).

Read pages (list/detail) are public — groups are community metadata. Write
routes are login_required; join/leave are POST-only (state changes are never
GETs). All rules (owner cannot leave, no duplicate membership, role ladder)
live in services — views only translate outcomes into messages.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.types import GroupKind

from . import services
from .forms import GroupForm
from .models import Group, GroupRole
from .services import OwnerCannotLeave

# HARD RULE (PROMPTS.md budgets): list surfaces show at most 20 items per page.
PAGE_SIZE = 20


def group_list(request):
    """Group list with kind filter (?kind=INTEREST|NEIGHBOURHOOD|PROFESSION).
    An unknown kind simply matches nothing — an empty page, never a 500."""
    kind = request.GET.get("kind", "")
    queryset = Group.objects.all().order_by("name")
    if kind:
        queryset = queryset.filter(kind=kind)
    page_obj = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "groups/list.html",
        {
            "groups": list(page_obj.object_list),
            "page_obj": page_obj,
            "kinds": GroupKind.choices,
            "active_kind": kind,
        },
    )


def group_detail(request, pk):
    """Group page: description + paged members list (≤20 per page)."""
    group = get_object_or_404(Group, pk=pk)
    rows = services.members_of(group)
    page_obj = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    my_role = services.user_role(request.user, group)
    return render(
        request,
        "groups/detail.html",
        {
            "group": group,
            "memberships": list(page_obj.object_list),
            "page_obj": page_obj,
            "member_count": services.member_count(group),
            "my_role": my_role.value if my_role else None,
            "is_moderator": services.is_group_moderator(request.user, group),
        },
    )


@login_required
@require_POST
def group_join(request, pk):
    group = get_object_or_404(Group, pk=pk)
    # Re-joining keeps the existing role (service guarantees no downgrade) —
    # so this route never demotes an owner or moderator who taps join twice.
    services.join(user=request.user, group=group)
    messages.success(
        request, _("Welcome to %(name)s.") % {"name": group.name}
    )
    return redirect("groups:detail", pk=pk)


@login_required
@require_POST
def group_leave(request, pk):
    group = get_object_or_404(Group, pk=pk)
    try:
        changed = services.leave(user=request.user, group=group)
    except OwnerCannotLeave:
        # Friendly block (not a 403): the message explains the rule.
        messages.error(request, _("The group owner cannot leave their own group."))
    else:
        if changed:
            messages.success(request, _("You left the group."))
    return redirect("groups:detail", pk=pk)


@login_required
def group_create(request):
    """Create a group: the creator becomes OWNER immediately (services note)."""
    if request.method == "POST":
        form = GroupForm(request.POST)
        if form.is_valid():
            try:
                group = services.create_group(
                    owner=request.user, **form.cleaned_data
                )
            except ValidationError as exc:
                # The service is the rulebook; surface its verdict inline.
                messages.error(request, "; ".join(str(msg) for msg in exc.messages))
            else:
                messages.success(
                    request, _("Group created — share it with your people.")
                )
                return redirect("groups:detail", pk=group.pk)
    else:
        form = GroupForm()
    return render(request, "groups/form.html", {"form": form, "creating": True})
