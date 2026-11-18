"""URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

from reports.views import report_conversation_user

from events.views import (
    EventDetailView,
    LandingView,
    NotificationListView,
    SearchView,
)
from events.bell_views import notification_bell
from events.messaging_views import (
    InboxView,
    conversation,
    conversation_updates,
    message_user,
    unread_message_count,
)

urlpatterns = [
    # Landing page at root "/" (login success target); lists upcoming events
    path("", LandingView.as_view(), name="landing"),
    path("search", SearchView.as_view(), name="search"),
    path("events/<int:pk>/", EventDetailView.as_view(), name="event_detail"),
    path("events/", include("events.urls")),
    path("messages/", InboxView.as_view(), name="inbox"),
    path(
        "messages/unread-count/",
        unread_message_count,
        name="unread_message_count",
    ),
    path(
        "messages/<int:user_id>/",
        message_user,
        name="message_user",
    ),
    path(
        "messages/conversations/<int:conversation_id>/",
        conversation,
        name="conversation",
    ),
    path(
        "messages/conversations/<int:conversation_id>/report/",
        report_conversation_user,
        name="conversation_report",
    ),
    path(
        "messages/conversations/<int:conversation_id>/updates/",
        conversation_updates,
        name="conversation_updates",
    ),
    path("notifications/", NotificationListView.as_view(), name="notifications"),
    path("notifications/bell/", notification_bell, name="notification_bell"),
    # Move home page to /home/, avoid conflicting with root
    path(
        "home/", TemplateView.as_view(template_name="accounts/home.html"), name="home"
    ),
    path("", include("chatbot.urls")),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    # Google social authentication; existing accounts routes take precedence.
    path("accounts/", include("allauth.urls")),
]
