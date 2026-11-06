"""Post views (Prompt 10) — list, detail, create, edit, soft-remove, upload.

Budget notes (mobile-first, 50 KB HTML): the list shows ≤10 posts per page
(guides-style pagination), photos render as small lazy thumbs in lists and
standard variants on the detail page — images are separate cacheable
requests, the HTML stays text-weight. No JavaScript needed for anything here.
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

from .forms import PostForm
from .models import MAX_IMAGES, MAX_POST_CHARS, MSG_TOO_MANY_IMAGES, Post
from .services import (
    _assert_author,
    add_image,
    create_post,
    edit_post,
    get_visible_post,
    post_image_count,
    reference_preview,
    remove_post,
    visible_posts,
)


def post_list(request):
    """Chronological published posts. ?tag=educational is the filter; unknown
    tags fall back to All (never a 500 on a stray querystring)."""
    tag = (request.GET.get("tag") or "").strip()
    active_tag = tag if tag == "educational" else ""
    posts = visible_posts(educational=True) if active_tag else visible_posts()
    paginator = Paginator(posts, 10)
    page_obj = paginator.get_page(request.GET.get("page"))
    # WHY per-item preview dicts, not a model property: the preview depends on
    # THE VIEWER (a guide can be visible to you and not to me). Computed here
    # with the request user, never inside the model.
    items = [
        {
            "post": post,
            "preview": (reference_preview(
                ref_guide=post.ref_guide, ref_listing=post.ref_listing,
                viewer=request.user)
                if (post.ref_guide_id or post.ref_listing) else None),
        }
        for post in page_obj.object_list
    ]
    return render(
        request,
        "posts/list.html",
        {"posts": items, "page_obj": page_obj, "active_tag": active_tag},
    )


def post_detail(request, pk: int):
    """The feed-item page (Prompt 10 budget test surface)."""
    post = get_visible_post(pk, request.user)
    if post is None:
        raise Http404("No such post")
    preview = reference_preview(
        ref_guide=post.ref_guide, ref_listing=post.ref_listing, viewer=request.user
    )
    return render(
        request,
        "posts/detail.html",
        {
            "post": post,
            "attachments": post.attachments.select_related("image"),
            "preview": preview,
            "is_author": post.author_id == request.user.pk,
        },
    )


@login_required
def post_create(request):
    form = PostForm(request.POST or None, viewer=request.user)
    if request.method == "POST" and form.is_valid():
        post = create_post(
            author=request.user,
            text=form.cleaned_data["text"],
            educational=form.cleaned_data["educational"],
            ref_guide=form.cleaned_data["ref_guide"],
        )
        messages.success(request, "Posted. / Imechapishwa.")
        return redirect("posts:detail", pk=post.pk)
    return render(request, "posts/form.html",
                  {"form": form, "max_chars": MAX_POST_CHARS})


@login_required
def post_edit(request, pk: int):
    post = get_object_or_404(Post, pk=pk)
    # Author (or staff) only — PermissionDenied raised here hits both the
    # GET (peeking at the form) and the POST (actually changing it).
    _assert_author(request.user, post, "edit")
    if request.method == "POST":
        form = PostForm(request.POST, viewer=request.user)
        if form.is_valid():
            edit_post(
                post=post, editor=request.user,
                text=form.cleaned_data["text"],
                educational=form.cleaned_data["educational"],
                ref_guide=form.cleaned_data["ref_guide"],
            )
            messages.success(request, "Saved. / Imehifadhiwa.")
            return redirect("posts:detail", pk=post.pk)
    else:
        form = PostForm(
            initial={
                "text": post.text,
                "educational": post.educational,
                "ref_guide": post.ref_guide_id or None,
            },
            viewer=request.user,
        )
    return render(request, "posts/form.html",
                  {"form": form, "post": post, "max_chars": MAX_POST_CHARS})


@login_required
def post_delete(request, pk: int):
    """Soft remove: status REMOVED (moderation evidence) — never hard delete."""
    post = get_object_or_404(Post, pk=pk)
    _assert_author(request.user, post, "remove")
    if request.method == "POST":
        remove_post(post, by_user=request.user)
        messages.success(request, "Post removed. / Chapisho limeondolewa.")
        return redirect("posts:list")
    return render(request, "posts/remove.html", {"post": post})


@require_POST
def post_image_upload(request, pk: int):
    """Upload endpoint for a post's photos. The 4-photo gate lives HERE as
    well as on the model (PROMPTS.md pitfall: client-side checks are
    decoration): 1) authenticated, 2) own post, 3) current photo count < 4,
    then the file goes through the Prompt 06 pipeline and service.add_image().

    WHY JSON responses: this endpoint is the photo-picker API used by the
    create page's little JS (and any future offline shell flow).
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "login required / ingia kwanza"}, status=403)
    try:
        post = Post.objects.get(pk=pk)
    except Post.DoesNotExist:
        return JsonResponse({"error": _("No such post.")}, status=404)
    if post.author_id != request.user.pk and not request.user.is_staff:
        return JsonResponse(
            {"error": _("Only the author can add photos.")}, status=403
        )
    # THE server-side cap — one truth shared with the model's clean().
    count = post_image_count(post)
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
        ref = save_upload(file_obj=upload, owner=request.user, purpose="post")
        row = add_image(post, ref)
    except images.InvalidImage as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except ValidationError as exc:
        # full_clean inside add_image is the second net — duplicate photo or
        # a simultaneous upload racing past the pre-check.
        first = exc.messages[0] if exc.messages else str(exc)
        return JsonResponse({"error": str(first)}, status=400)
    return JsonResponse(
        {
            "uuid": str(ref.id),
            "position": row.position,
            "count": post_image_count(post),
            "standard": {"path": ref.standard_path,
                          "bytes": ref.standard_bytes,
                          "w": ref.standard_w, "h": ref.standard_h},
            "thumb": {"path": ref.thumb_path, "bytes": ref.thumb_bytes,
                      "w": ref.thumb_w, "h": ref.thumb_h},
        },
        status=201,
    )
