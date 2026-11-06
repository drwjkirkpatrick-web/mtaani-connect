"""Prompt 21 — Barter routes (app mounted at /barter/ by root urls.py).

app_name is exactly 'barter'. Every route is login_required in the views
(the inbox IS the app); state-changing routes are POST-only (CSRF-checked)
— state moves are never link-followed.
"""
from django.urls import path

from . import views

app_name = "barter"

urlpatterns = [
    path("", views.offer_list, name="list"),
    path("new/", views.offer_new, name="new"),
    path("<int:pk>/", views.offer_detail, name="detail"),
    # Draft workspace (DRAFT-only, initiator-only — services enforce).
    path("<int:pk>/edit/", views.offer_edit, name="edit"),
    # THE STATE MOVES — POST only; views never write status fields.
    path("<int:pk>/send/", views.offer_send, name="send"),
    path("<int:pk>/accept/", views.offer_accept, name="accept"),
    path("<int:pk>/cancel/", views.offer_cancel, name="cancel"),
    path("<int:pk>/complete/", views.offer_complete, name="complete"),
    # Start a counteroffer on the offer on the table.
    path("<int:pk>/counter/", views.offer_counter, name="counter"),
]
