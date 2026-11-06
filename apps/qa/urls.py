from django.urls import path

from . import views

app_name = "qa"

# NOTE: every name lives in the qa namespace; root urls.py includes this
# module at /qa/. Accept and answer routes are named post-views (login).
urlpatterns = [
    path("", views.question_list, name="list"),
    path("ask/", views.question_ask, name="ask"),
    path("<int:pk>/", views.question_detail, name="detail"),
    path("<int:pk>/answer/", views.answer_question, name="answer"),
    path("<int:q_pk>/accept/<int:a_pk>/", views.accept_answer, name="accept"),
    path("answers/<int:pk>/vote/", views.answer_vote, name="vote"),
]
