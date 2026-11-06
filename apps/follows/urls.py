"""Follow routes (PROMPTS.md Prompt 13)."""
from django.urls import path

from . import views

app_name = "follows"

urlpatterns = [
    path("<int:user_id>/follow/", views.follow_action, name="follow"),
    path("<int:user_id>/unfollow/", views.unfollow_action, name="unfollow"),
    path("<int:user_id>/following/", views.following_list, name="following"),
    path("<int:user_id>/followers/", views.followers_list, name="followers"),
]
