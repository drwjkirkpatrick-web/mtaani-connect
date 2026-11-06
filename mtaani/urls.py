"""Root URLconf. Placeholder home until Prompt 05's design system lands."""
from django.urls import include, path
from django.views.generic import TemplateView

from . import views

urlpatterns = [
    path("", TemplateView.as_view(template_name="home.html"), name="home"),
    path("accounts/", include("apps.accounts.urls")),
    path("media/", include("apps.media.urls")),
    path("profiles/", include("apps.profiles.urls")),
    # Prompt 05: language toggle (cookie-persisted), offline shell + module stubs.
    path("lang/<str:code>/", views.set_lang, name="set_lang"),
    path("offline/", TemplateView.as_view(template_name="offline.html"), name="offline"),
    # Prompt 08: Guides take over /guides/ (stub removed).
    path("guides/", include("apps.guides.urls")),
    path("posts/", include("apps.posts.urls")),
    path("qa/", include("apps.qa.urls")),
    path("follows/", include("apps.follows.urls")),
    path("groups/", include("apps.groups.urls")),
    path("moderation/", include("apps.moderation.urls")),
    path("feed/", include("apps.feed.urls")),
    path("search/", include("apps.search.urls")),
    path("trust/", include("apps.trust.urls")),
    # Prompt 17/20 take over the market + chats stubs.
    path("market/", include("apps.listings.urls")),
    path("chats/", include("apps.messaging.urls")),
    path("services/", include("apps.services.urls")),
    # NOTE: stub route is a placeholder until its app takes over with include():
    # Transport (25).
    path("transport/", TemplateView.as_view(template_name="stub.html"), name="transport_stub"),
]
