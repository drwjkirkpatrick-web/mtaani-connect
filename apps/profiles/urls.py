"""Profile routes (Prompt 07)."""
from django.urls import path

from . import views

app_name = "profiles"

urlpatterns = [
    path("u/<str:username>/", views.profile_view, name="profile"),
    path("me/", views.my_profile, name="me"),
    path("me/privacy/", views.my_privacy, name="me_privacy"),
    path("me/avatar/", views.my_avatar, name="me_avatar"),
]
