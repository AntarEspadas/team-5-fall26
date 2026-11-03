"""Seed the FAQ with the site's support answers; staff can edit them later."""

from django.db import migrations

FAQS = [
    (
        "booking",
        "How do I book or register for an event?",
        "This site doesn't sell tickets. Sign in, open the event, and choose "
        "Want to Attend (or Interested) to save it to your plans and get "
        "updates from the host. If the event page has a registration link, the "
        "organizer also needs you to register on their site.",
    ),
    (
        "booking",
        "Do I need a ticket?",
        "Most NYC Parks events are free and open to everyone without a ticket. "
        "If an event needs registration or has a fee, its description or "
        "registration link says so.",
    ),
    (
        "booking",
        "Where can I see my bookings?",
        'Ask the assistant "When is my next event?" or "What events am I '
        'going to?" to list the events you chose Want to Attend or Interested '
        "on. Events you host are on My events (/events/mine/).",
    ),
    (
        "booking",
        "Is there space left at an event?",
        "The site doesn't track capacity. An event is open while it is "
        "upcoming and not cancelled; its page shows how many people are "
        "interested or want to attend. For events with limited places, check "
        "the organizer's registration link.",
    ),
    (
        "cancellations",
        "How do I cancel my booking?",
        "Open the event and click your selected option (Want to Attend or "
        "Interested) again to remove it, or choose the other option to switch. "
        "If you registered on the organizer's site, cancel there too.",
    ),
    (
        "cancellations",
        "What happens if an event is cancelled?",
        "When a host cancels, everyone who chose Interested or Want to Attend "
        "gets an email and a notification, and the event page shows a "
        "cancellation banner. Cancelled events leave search and the home page.",
    ),
    (
        "cancellations",
        "Can I get a refund?",
        "The site doesn't take payments, so there is nothing to refund here. "
        "If you paid an organizer through their registration page, contact "
        "that organizer about refunds.",
    ),
    (
        "cancellations",
        "How do I cancel an event I'm hosting?",
        "Open your event and choose Cancel event (or use My events). You can "
        "add a message for attendees. Cancelling can't be undone.",
    ),
    (
        "payments",
        "How much do events cost?",
        "Most NYC Parks events are free. The site doesn't store prices or take "
        "payments; any fee is in the event description or on the organizer's "
        "registration page.",
    ),
    (
        "payments",
        "I have a payment problem or an unexpected charge.",
        "We never charge you on this site. For a charge from an organizer's "
        "registration page, contact that organizer. If something still looks "
        "wrong, contact our support team.",
    ),
    (
        "account",
        "I forgot my password.",
        "Use Forgot password on the login page (/accounts/password-reset/) to "
        "get a reset link by email. Check your spam folder if it doesn't "
        "arrive.",
    ),
    (
        "account",
        "I can't log in.",
        "You can log in with your username or your email address and password. "
        "If you signed up with Google, use Continue with Google. If you still "
        "can't get in, reset your password at /accounts/password-reset/.",
    ),
    (
        "account",
        "How do I change my profile or privacy settings?",
        "Go to Account settings (/accounts/settings/) to change your display "
        "name, photo, bio, pronouns, interests, who can contact you, and "
        "whether other attendees can see your profile.",
    ),
    (
        "account",
        "Why was I logged out?",
        "For security you're signed out after 48 hours without activity. Log "
        "in again at /accounts/login/.",
    ),
    (
        "events",
        "Where do the events come from?",
        "Events come from the official NYC Parks events feed, refreshed every "
        "hour, and from members who post their own events.",
    ),
    (
        "events",
        "How do I find the venue?",
        "Each event page lists the park and location. NYC Parks events also "
        "link to their official NYC Parks page with more details.",
    ),
]


def seed(apps, schema_editor):
    FAQ = apps.get_model("chatbot", "FAQ")
    FAQ.objects.bulk_create(
        FAQ(topic=topic, question=question, answer=answer, position=index)
        for index, (topic, question, answer) in enumerate(FAQS)
    )


def unseed(apps, schema_editor):
    FAQ = apps.get_model("chatbot", "FAQ")
    FAQ.objects.filter(question__in=[question for _, question, _ in FAQS]).delete()


class Migration(migrations.Migration):

    dependencies = [("chatbot", "0002_faq_chatlog")]

    operations = [migrations.RunPython(seed, unseed)]
