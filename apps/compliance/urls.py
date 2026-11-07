"""Compliance routes — Prompt 40 (KDPA operations), app_name = 'compliance'.

Included by the parent at /compliance/ (mtaani/urls.py). NOTES:
* "" (dashboard) is the auditor page — full DataMap + the DSAR queue.
* "me/" + "me/export/" are the member's self-serve doors.
* "requests/" is the staff queue; the per-request page carries the
  fulfil/refuse actions (DELETE fulfilment runs anonymize_account).
* "consents/" + "consents/export/" are the staff consent-ledger views
  (timeline by username; CSV-ish export for the ODPC demonstration).
"""
from django.urls import path

from . import views

app_name = "compliance"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("me/", views.my_data, name="my_data"),
    path("me/export/", views.export_my_data, name="export_my_data"),
    path("requests/", views.requests_view, name="requests"),
    path("requests/<int:pk>/", views.request_detail, name="request_detail"),
    path("consents/", views.user_consents, name="user_consents"),
    path("consents/export/", views.consent_export, name="consent_export"),
]
