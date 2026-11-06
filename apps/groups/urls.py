"""Group routes (PROMPTS.md Prompt 13)."""
from django.urls import path

from . import views

app_name = "groups"

urlpatterns = [
    path("", views.group_list, name="list"),
    path("new/", views.group_create, name="create"),
    path("<int:pk>/", views.group_detail, name="detail"),
    path("<int:pk>/join/", views.group_join, name="join"),
    path("<int:pk>/leave/", views.group_leave, name="leave"),
]
