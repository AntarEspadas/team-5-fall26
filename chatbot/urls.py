"""URL routes for the website assistant."""

from django.urls import path

from . import views

app_name = "chatbot"

urlpatterns = [
    path("message/", views.chat_message, name="message"),
    path("voice/", views.chat_voice, name="voice"),
    path("history/", views.chat_history, name="history"),
    path("reset/", views.chat_reset, name="reset"),
    path("inquiries/", views.inquiry_list, name="inquiries"),
    path(
        "inquiries/<int:pk>/answer/",
        views.answer_inquiry_view,
        name="answer_inquiry",
    ),
]
