"""Profile views (Prompt 07): public profile, self edit, privacy, avatar."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.core import images
from apps.media.services import save_upload

from .forms import PrivacyForm, ProfileForm
from .models import GROUPS
from .services import get_profile, public_data


def profile_view(request, username):
    from django.contrib.auth import get_user_model

    user = get_object_or_404(get_user_model(), username=username)
    profile = get_profile(user)
    data = public_data(profile, viewer_is_member=request.user.is_authenticated)
    return render(request, "profiles/profile.html", {"p": data})


@login_required
def my_profile(request):
    profile = get_profile(request.user)
    if request.method == "POST":
        form = ProfileForm(request.POST)
        if form.is_valid():
            profile.display_name = form.cleaned_data["display_name"]
            profile.bio = form.cleaned_data["bio"]
            profile.skills = form.cleaned_data["skills"]
            profile.interests = form.cleaned_data["interests"]
            profile.ward = form.cleaned_data["ward"]
            profile.save()
            messages.success(request, "Saved. / Imehifadhiwa.")
            return redirect("profiles:me")
    else:
        initial = {
            "display_name": profile.display_name,
            "bio": profile.bio,
            "skills": ", ".join(profile.skills),
            "interests": ", ".join(profile.interests),
            "ward": profile.ward,
        }
        form = ProfileForm(initial=initial)
    return render(request, "profiles/edit.html", {"form": form, "profile": profile})


@login_required
def my_privacy(request):
    profile = get_profile(request.user)
    if request.method == "POST":
        form = PrivacyForm(request.POST)
        if form.is_valid():
            profile.location_visibility = form.cleaned_data["location_visibility"]
            profile.field_visibility = {
                "bio": form.cleaned_data["bio_visibility"],
                "skills": form.cleaned_data["skills_visibility"],
                "interests": form.cleaned_data["interests_visibility"],
                "location": form.cleaned_data["location_group_visibility"],
            }
            profile.save()
            messages.success(request, "Privacy settings saved. / Mipangilio imehifadhiwa.")
            return redirect("profiles:me_privacy")
    else:
        vis = {g: profile.visibility_for(g) for g in GROUPS}
        initial = {
            "location_visibility": profile.location_visibility,
            "bio_visibility": vis["bio"],
            "skills_visibility": vis["skills"],
            "interests_visibility": vis["interests"],
            "location_group_visibility": vis["location"],
        }
        form = PrivacyForm(initial=initial)
    return render(request, "profiles/privacy.html", {"form": form})


@login_required
def my_avatar(request):
    """Avatar flows through the Prompt 06 pipeline like every other image."""
    if request.method == "POST":
        upload = request.FILES.get("image")
        if upload is None:
            messages.error(request, "Pick an image first. / Chagua picha kwanza.")
        else:
            try:
                ref = save_upload(file_obj=upload, owner=request.user, purpose="profile")
            except images.InvalidImage as exc:
                messages.error(request, str(exc))
            else:
                profile = get_profile(request.user)
                profile.avatar = ref
                profile.save(update_fields=["avatar", "updated_at"])
                messages.success(request, "Avatar updated. / Picha imebadilishwa.")
    return redirect("profiles:me")
