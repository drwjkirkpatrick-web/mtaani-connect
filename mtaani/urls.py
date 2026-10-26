"""Root URLconf. Placeholder home until Prompt 05's design system lands."""
from django.urls import path
from django.views.generic import TemplateView

urlpatterns = [
    path("", TemplateView.as_view(template_name="home.html"), name="home"),
]
