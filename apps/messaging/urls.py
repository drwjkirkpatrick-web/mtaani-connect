"""Prompt 20 — Messaging v1 routes (app the root urls.py mounts at /chats/).

The base.html Chats tab links messaging:list — that route name is EXACTLY
'list' (parent contract). All member routes are login_required in the views.
"""
from django.urls import path

from . import views

app_name = "messaging"

urlpatterns = [
    path("", views.thread_list, name="list"),  # the exact name base.html needs
    path("start/", views.start_for_listing, name="start_for_listing"),
    path("privacy/", views.privacy_note, name="privacy"),
    path("<int:pk>/", views.thread_detail, name="detail"),
    path("<int:pk>/mute/", views.thread_mute, name="mute"),
    path("<int:pk>/unmute/", views.thread_unmute, name="unmute"),
    path("<int:pk>/receipts/", views.receipt_settings, name="receipts"),
    path("<int:pk>/report/", views.report_message, name="report_message"),
]
