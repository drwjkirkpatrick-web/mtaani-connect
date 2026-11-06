"""URL routes for the Posts app (Prompt 10).

app_name = "posts" — templates and tests reverse names like `posts:list`.
mtaani/urls.py already includes this module under /posts/.

Route set (final report ships these names to the parent + Feed Prompt 11):
  list          chronological published posts (?tag=educational filter)
  detail        one post — the feed-item page
  create        new post (login)
  edit          edit a post (author-only)
  image_upload  POST-only photo upload endpoint (4-image cap server-side)
  delete        soft remove (status REMOVED, author-only, POST-only)
"""
from django.urls import path

from . import views

app_name = "posts"

urlpatterns = [
    path("", views.post_list, name="list"),
    path("new/", views.post_create, name="create"),
    path("<int:pk>/", views.post_detail, name="detail"),
    path("<int:pk>/edit/", views.post_edit, name="edit"),
    path("<int:pk>/images/upload/", views.post_image_upload, name="image_upload"),
    path("<int:pk>/delete/", views.post_delete, name="delete"),
]
