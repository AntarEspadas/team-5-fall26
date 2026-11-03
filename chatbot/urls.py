"""URL routes for the website assistant."""

from django.urls import path

from . import views

app_name = "chatbot"

urlpatterns = [
    # Chat API used by the widget.
    path("api/chat", views.chat_message, name="message"),
    path("api/chat/voice", views.chat_voice, name="voice"),
    path("api/chat/history", views.chat_history, name="history"),
    path("api/chat/reset", views.chat_reset, name="reset"),
    # Page where event managers answer forwarded questions.
    path("assistant/inquiries/", views.inquiry_list, name="inquiries"),
    path(
        "assistant/inquiries/<int:pk>/answer/",
        views.answer_inquiry_view,
        name="answer_inquiry",
    ),
]
