"""Root URLconf. Placeholder home until Prompt 05's design system lands."""
from django.urls import include, path
from django.views.generic import TemplateView

from . import views

urlpatterns = [
    path("", TemplateView.as_view(template_name="home.html"), name="home"),
    path("accounts/", include("apps.accounts.urls")),
    path("media/", include("apps.media.urls")),
    # Prompt 05: language toggle (cookie-persisted), offline shell + module stubs.
    path("lang/<str:code>/", views.set_lang, name="set_lang"),
    path("offline/", TemplateView.as_view(template_name="offline.html"), name="offline"),
    # NOTE: stub routes are placeholders until each app takes over with include():
    # Guides (08), Market (17), Chats (20), Transport (25).
    path("guides/", TemplateView.as_view(template_name="stub.html"), name="guides_stub"),
    path("market/", TemplateView.as_view(template_name="stub.html"), name="market_stub"),
    path("chats/", TemplateView.as_view(template_name="stub.html"), name="chats_stub"),
    path("transport/", TemplateView.as_view(template_name="stub.html"), name="transport_stub"),
]
