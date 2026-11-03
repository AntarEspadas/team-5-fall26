"""Tests for the chat endpoints, the widget, and the Questions page."""

import json
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from chatbot import assistant, gemini, views
from chatbot.admin import ChatLogAdmin, InquiryAdmin
from chatbot.inquiries import answer_inquiry, create_inquiry
from chatbot.models import FAQ, ChatLog, Inquiry
from chatbot.tests.helpers import call_response, make_event, make_user, text_response
from events.models import Notification

GENERATE = "chatbot.gemini.generate_content"
TRANSCRIBE = "chatbot.gemini.transcribe"
ENABLED = override_settings(CHATBOT_ENABLED=True, GEMINI_API_KEY="test-key")


def wav(size=64):
    """Return a small fake WAV upload."""
    return SimpleUploadedFile("voice.wav", b"R" * size, content_type="audio/wav")


class ChatTestCase(TestCase):
    """Base class: the assistant is on and rate limits start fresh."""

    def setUp(self):
        cache.clear()

    def post_message(self, message, page=""):
        return self.client.post(
            reverse("chatbot:message"),
            json.dumps({"message": message, "page": page}),
            content_type="application/json",
        )


@ENABLED
class ChatMessageTests(ChatTestCase):
    """Typed messages run the tool loop and keep a short history."""

    def test_answers_with_tool_results(self):
        event = make_event("y1", "Sunrise Yoga")
        responses = [
            call_response("search_events", query="yoga"),
            text_response("**Sunrise Yoga** is on — see /events/1/"),
        ]
        with mock.patch(GENERATE, side_effect=responses) as generate:
            response = self.post_message("Any yoga?", page=f"/events/{event.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "reply": "**Sunrise Yoga** is on — see /events/1/",
                "escalated": False,
                "declined": False,
            },
        )
        second = generate.call_args_list[1].args[0]
        self.assertIn("Sunrise Yoga", second["systemInstruction"]["parts"][0]["text"])
        tool_reply = second["contents"][-1]["parts"][0]["functionResponse"]
        self.assertEqual(tool_reply["name"], "search_events")
        self.assertEqual(
            tool_reply["response"]["result"]["events"][0]["title"], "Sunrise Yoga"
        )
        self.assertEqual(
            self.client.session[assistant.HISTORY_KEY],
            [
                {"role": "user", "text": "Any yoga?"},
                {"role": "model", "text": "**Sunrise Yoga** is on — see /events/1/"},
            ],
        )

    def test_history_is_sent_and_trimmed(self):
        session = self.client.session
        session[assistant.HISTORY_KEY] = [
            {"role": "user", "text": f"m{i}"} for i in range(assistant.HISTORY_LIMIT)
        ] + [{"role": "system", "text": "ignored"}]
        session.save()
        with mock.patch(GENERATE, return_value=text_response("Hi")) as generate:
            self.post_message("Hello")
        contents = generate.call_args.args[0]["contents"]
        self.assertEqual(len(contents), assistant.HISTORY_LIMIT + 1)
        self.assertEqual(
            len(self.client.session[assistant.HISTORY_KEY]), assistant.HISTORY_LIMIT
        )

    def test_corrupt_history_is_ignored(self):
        session = self.client.session
        session[assistant.HISTORY_KEY] = "junk"
        session.save()
        with mock.patch(GENERATE, return_value=text_response("Hi")):
            self.assertEqual(self.post_message("Hello").status_code, 200)

    def test_escalation_is_reported(self):
        responses = [
            call_response(
                "escalate_to_event_manager",
                question="Is it free?",
                question_english="Is it free?",
                language="English",
            ),
            text_response("I'll check with the event manager and let you know."),
        ]
        with mock.patch(GENERATE, side_effect=responses):
            response = self.post_message("Is it free?")
        self.assertTrue(response.json()["escalated"])
        inquiry = Inquiry.objects.get()
        self.assertEqual(self.client.session[assistant.INQUIRIES_KEY], [inquiry.pk])

    def test_silent_model_escalates_automatically(self):
        event = make_event("e1", "Swim")
        with mock.patch(GENERATE, return_value=text_response("")):
            response = self.post_message("Pool depth?", page=f"/events/{event.pk}/")
        self.assertEqual(response.json()["reply"], assistant.escalated_reply())
        self.assertEqual(Inquiry.objects.get().event, event)

    def test_endless_tool_calls_escalate(self):
        with mock.patch(GENERATE, return_value=call_response("get_my_activity")):
            response = self.post_message("Loop")
        self.assertTrue(response.json()["escalated"])

    def test_blocked_prompts_get_a_polite_refusal(self):
        with mock.patch(GENERATE, return_value={"promptFeedback": {}}):
            response = self.post_message("Something bad")
        self.assertEqual(response.json()["reply"], assistant.BLOCKED_REPLY)

    def test_gemini_failures_return_503(self):
        with mock.patch(GENERATE, side_effect=gemini.GeminiError("down")):
            response = self.post_message("Hello")
        self.assertEqual(response.status_code, 503)

    def test_invalid_requests(self):
        url = reverse("chatbot:message")
        bad = [
            self.client.post(url, "not json", content_type="application/json"),
            self.client.post(url, "[1]", content_type="application/json"),
            self.post_message("   "),
            self.post_message("x" * 1001),
        ]
        self.assertEqual([r.status_code for r in bad], [400] * 4)
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_rate_limit(self):
        with mock.patch(GENERATE, return_value=text_response("Hi")):
            statuses = [self.post_message("Hi").status_code for _ in range(16)]
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(statuses.count(200), 15)

    def test_rate_limit_per_network_address(self):
        with mock.patch("chatbot.views.IP_RATE_LIMIT", 1):
            with mock.patch(GENERATE, return_value=text_response("Hi")):
                first = self.post_message("Hi")
                second = self.client.post(
                    reverse("chatbot:message"),
                    json.dumps({"message": "Hi"}),
                    content_type="application/json",
                    HTTP_X_FORWARDED_FOR="127.0.0.1, 10.0.0.1",
                )
        self.assertEqual((first.status_code, second.status_code), (200, 429))

    def test_rate_limit_survives_expired_counters(self):
        with mock.patch("chatbot.views.cache.incr", side_effect=ValueError):
            with mock.patch(GENERATE, return_value=text_response("Hi")):
                self.assertEqual(self.post_message("Hi").status_code, 200)

    def test_signed_in_users_are_rate_limited_by_account(self):
        user = make_user("chatty")
        self.client.force_login(user)
        with mock.patch(GENERATE, return_value=text_response("Hi")) as generate:
            self.post_message("Hi")
        prompt = generate.call_args.args[0]["systemInstruction"]["parts"][0]["text"]
        self.assertIn("signed in as chatty", prompt)
        self.assertTrue(cache.get(f"chatbot-rate:user:{user.pk}"))


class DisabledAssistantTests(ChatTestCase):
    """Without an API key the endpoints and the widget are hidden."""

    def test_endpoints_404_and_widget_hidden(self):
        self.assertEqual(self.post_message("Hi").status_code, 404)
        response = self.client.get(reverse("login"))
        self.assertNotContains(response, "parks-assistant")


@ENABLED
class WidgetTests(ChatTestCase):
    """The widget is on every page when enabled."""

    def test_widget_on_public_and_signed_in_pages(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, 'id="parks-assistant"')
        self.assertContains(response, "How do I sign up?")
        self.client.force_login(make_user("viewer"))
        response = self.client.get(reverse("landing"))
        self.assertContains(response, "chatbot/chatbot.js")
        self.assertContains(response, "What events am I going to?")


@ENABLED
class ChatVoiceTests(ChatTestCase):
    """Voice messages are transcribed, then answered like typed ones."""

    def post_voice(self, audio):
        data = {"page": "/"}
        if audio is not None:
            data["audio"] = audio
        return self.client.post(reverse("chatbot:voice"), data)

    def test_transcribes_and_answers(self):
        with mock.patch(TRANSCRIBE, return_value=("¿Qué hay hoy?", "es-ES")):
            with mock.patch(GENERATE, return_value=text_response("Hoy hay yoga.")):
                response = self.post_voice(wav())
        self.assertEqual(
            response.json(),
            {
                "reply": "Hoy hay yoga.",
                "escalated": False,
                "declined": False,
                "transcript": "¿Qué hay hoy?",
                "language": "es-ES",
            },
        )

    def test_rejects_bad_uploads(self):
        too_big = SimpleUploadedFile(
            "a.wav", b"0" * (4 * 1024 * 1024 + 1), content_type="audio/wav"
        )
        text = SimpleUploadedFile("a.txt", b"hi", content_type="text/plain")
        statuses = [self.post_voice(a).status_code for a in (None, too_big, text)]
        self.assertEqual(statuses, [400, 400, 400])

    def test_unheard_audio(self):
        with mock.patch(TRANSCRIBE, return_value=("", "")):
            self.assertEqual(self.post_voice(wav()).status_code, 422)

    def test_transcription_failure(self):
        with mock.patch(TRANSCRIBE, side_effect=gemini.GeminiError("x")):
            self.assertEqual(self.post_voice(wav()).status_code, 503)

    def test_answer_failure(self):
        with mock.patch(TRANSCRIBE, return_value=("Hello", "en-US")):
            with mock.patch(GENERATE, side_effect=gemini.GeminiError("x")):
                self.assertEqual(self.post_voice(wav()).status_code, 503)


@ENABLED
class HistoryAndResetTests(ChatTestCase):
    """History restores the chat and delivers managers' answers once."""

    def test_answers_are_delivered_into_the_chat_once(self):
        anonymous = create_inquiry(None, "Dogs allowed?")
        session = self.client.session
        session[assistant.INQUIRIES_KEY] = [anonymous.pk]
        session.save()

        data = self.client.get(reverse("chatbot:history")).json()
        self.assertEqual(data, {"messages": [], "updates": 0, "pending": True})

        answer_inquiry(anonymous, "Yes, on a leash.", make_user("staff"))
        data = self.client.get(reverse("chatbot:history")).json()
        self.assertEqual(data["updates"], 1)
        self.assertFalse(data["pending"])
        self.assertIn("Yes, on a leash.", data["messages"][0]["text"])

        data = self.client.get(reverse("chatbot:history")).json()
        self.assertEqual(data["updates"], 0)
        self.assertEqual(len(data["messages"]), 1)

    def test_signed_in_users_get_answers_from_any_session(self):
        user = make_user("asker")
        inquiry = create_inquiry(user, "Parking?")
        answer_inquiry(inquiry, "Street parking only.", make_user("staff"))
        self.client.force_login(user)
        data = self.client.get(reverse("chatbot:history")).json()
        self.assertEqual(data["updates"], 1)

    def test_reset_clears_the_conversation(self):
        session = self.client.session
        session[assistant.HISTORY_KEY] = [{"role": "user", "text": "Hi"}]
        session.save()
        response = self.client.post(reverse("chatbot:reset"))
        self.assertEqual(response.json(), {"messages": []})
        self.assertEqual(self.client.session[assistant.HISTORY_KEY], [])


class InquiryPageTests(TestCase):
    """Hosts answer questions about their events; staff answer all."""

    @classmethod
    def setUpTestData(cls):
        cls.host = make_user("host")
        cls.staff = make_user("staff", is_staff=True)
        cls.asker = make_user("asker")
        cls.event = make_event("host-1", "Kite Day", host=cls.host)
        cls.hosted = create_inquiry(
            cls.asker,
            "¿Hay baños?",
            question_english="Are there restrooms?",
            language="Spanish",
            event=cls.event,
        )
        cls.general = create_inquiry(None, "Is the site free?")

    def test_login_required(self):
        response = self.client.get(reverse("chatbot:inquiries"))
        self.assertEqual(response.status_code, 302)

    def test_hosts_see_questions_about_their_events(self):
        self.client.force_login(self.host)
        response = self.client.get(reverse("chatbot:inquiries"))
        self.assertContains(response, "Are there restrooms?")
        self.assertContains(response, "Asked in Spanish")
        self.assertNotContains(response, "Is the site free?")

    def test_staff_see_everything(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("chatbot:inquiries"))
        self.assertContains(response, "Are there restrooms?")
        self.assertContains(response, "Is the site free?")

    def test_answering_translates_and_notifies_the_asker(self):
        self.client.force_login(self.host)
        url = reverse("chatbot:answer_inquiry", args=[self.hosted.pk])
        mail.outbox.clear()
        with mock.patch(
            "chatbot.gemini.translate", return_value="Sí, junto al lago."
        ) as translate:
            response = self.client.post(url, {"answer": "Yes, by the lake."})
        self.assertRedirects(response, reverse("chatbot:inquiries"))
        translate.assert_called_once_with("Yes, by the lake.", "Spanish")
        self.hosted.refresh_from_db()
        self.assertEqual(self.hosted.status, Inquiry.Status.ANSWERED)
        self.assertEqual(self.hosted.answer_for_asker, "Sí, junto al lago.")
        notification = Notification.objects.get(recipient=self.asker)
        self.assertIn("Sí, junto al lago.", notification.body)
        self.assertEqual(mail.outbox[0].to, ["asker@example.com"])

        response = self.client.post(url, {"answer": "Again"}, follow=True)
        self.assertContains(response, "already answered")
        response = self.client.get(reverse("chatbot:inquiries"))
        self.assertContains(response, "Answer from host")

    def test_empty_answers_are_rejected(self):
        self.client.force_login(self.host)
        url = reverse("chatbot:answer_inquiry", args=[self.hosted.pk])
        response = self.client.post(url, {"answer": "  "}, follow=True)
        self.assertContains(response, "Write an answer")

    def test_other_users_cannot_answer(self):
        self.client.force_login(self.asker)
        url = reverse("chatbot:answer_inquiry", args=[self.hosted.pk])
        self.assertEqual(self.client.post(url, {"answer": "Hi"}).status_code, 404)

    def test_translation_failures_still_send_the_answer(self):
        with mock.patch("chatbot.gemini.translate", side_effect=gemini.GeminiError):
            self.assertTrue(answer_inquiry(self.hosted, "Yes.", self.host))
        self.hosted.refresh_from_db()
        self.assertEqual(self.hosted.answer_for_asker, "Yes.")


class InquiryAdminTests(TestCase):
    """Answering in the admin sends the answer like the Questions page."""

    def setUp(self):
        self.admin = InquiryAdmin(Inquiry, AdminSite())
        self.staff = make_user("admin", is_staff=True)
        self.request = RequestFactory().post("/")
        self.request.user = self.staff

    def test_no_adding_and_answer_on_save(self):
        self.assertFalse(self.admin.has_add_permission(self.request))
        inquiry = create_inquiry(None, "Open late?")
        self.assertEqual(str(inquiry), "Open late?")
        inquiry.answer = " Until 9 PM. "
        self.admin.save_model(self.request, inquiry, None, True)
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.status, Inquiry.Status.ANSWERED)
        self.assertEqual(inquiry.answer, "Until 9 PM.")
        self.assertEqual(inquiry.answered_by, self.staff)

        inquiry.answer = "Until 10 PM."
        self.admin.save_model(self.request, inquiry, None, True)
        inquiry.refresh_from_db()
        self.assertEqual(inquiry.answer, "Until 10 PM.")


class EventOnPageTests(TestCase):
    """Only event detail pages give the assistant page context."""

    def test_resolves_event_pages_only(self):
        event = make_event("e1", "Swim")
        self.assertEqual(assistant.event_on_page(f"/events/{event.pk}/?x=1"), event)
        self.assertIsNone(assistant.event_on_page("/search"))
        self.assertIsNone(assistant.event_on_page("/nowhere/"))
        self.assertIsNone(assistant.event_on_page(None))


@ENABLED
class ChatApiEndpointTests(ChatTestCase):
    """The user story's /api/chat contract: success, API failure, empty input."""

    URL = "/api/chat"

    def post(self, body):
        return self.client.post(self.URL, body, content_type="application/json")

    def test_url(self):
        self.assertEqual(reverse("chatbot:message"), self.URL)

    def test_success(self):
        with mock.patch(GENERATE, return_value=text_response("Hello!")):
            response = self.post({"message": "Hi"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "Hello!")

    def test_api_failure_returns_a_friendly_error(self):
        with mock.patch(GENERATE, side_effect=gemini.GeminiError("down")):
            response = self.post({"message": "Hi"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": views.UNAVAILABLE})

    def test_gemini_rate_limit_returns_a_friendly_error(self):
        error = gemini.GeminiError("quota", rate_limited=True)
        with mock.patch(GENERATE, side_effect=error):
            response = self.post({"message": "Hi"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": views.BUSY})
        with mock.patch(TRANSCRIBE, side_effect=error):
            response = self.client.post(
                reverse("chatbot:voice"), {"audio": wav(), "page": "/"}
            )
        self.assertEqual(response.json(), {"error": views.BUSY})

    def test_empty_input(self):
        for body in ({"message": ""}, {"message": "   "}, {}):
            response = self.post(body)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"error": "Type a message first."})

    def test_api_key_never_reaches_the_page(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, 'data-message-url="/api/chat"')
        self.assertNotContains(response, "test-key")


@ENABLED
class VoiceCallTests(ChatTestCase):
    """Voice calls get a locked token, run tools here, and save each turn."""

    def post_json(self, name, body):
        return self.client.post(
            reverse(name), json.dumps(body), content_type="application/json"
        )

    def test_session_returns_a_token_for_the_voice_setup(self):
        make_event("live-1", "Jazz Night")
        self.post_json("chatbot:live_turn", {"message": "Hi", "reply": "Hello!"})
        with mock.patch(
            "chatbot.gemini.create_live_token", return_value="auth_tokens/t"
        ) as create:
            response = self.post_json("chatbot:live", {"page": "/"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"], "auth_tokens/t")
        self.assertTrue(response.json()["url"].startswith("wss://"))
        setup = create.call_args.args[0]
        voice = setup["generationConfig"]["speechConfig"]
        self.assertEqual(voice["languageCode"], "en-US")
        self.assertEqual(
            voice["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Achernar"
        )
        self.assertIn("inputAudioTranscription", setup)
        self.assertIn("outputAudioTranscription", setup)
        prompt = setup["systemInstruction"]["parts"][0]["text"]
        self.assertIn("LIVE VOICE CALL", prompt)
        self.assertIn("Visitor: Hi", prompt)
        self.assertNotContains(response, "test-key")

    def test_session_failure_is_friendly(self):
        with mock.patch(
            "chatbot.gemini.create_live_token", side_effect=gemini.GeminiError("x")
        ):
            response = self.post_json("chatbot:live", {})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": views.UNAVAILABLE})

    def test_tools_run_for_the_visitor(self):
        make_event("live-2", "Sunrise Yoga")
        response = self.post_json(
            "chatbot:live_tool", {"name": "search_events", "args": {"query": "yoga"}}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sunrise Yoga", json.dumps(response.json()["result"]))
        self.assertFalse(response.json()["escalated"])

    def test_escalation_is_remembered_for_the_answer(self):
        response = self.post_json(
            "chatbot:live_tool",
            {"name": "escalate_to_event_manager", "args": {"question": "Dogs?"}},
        )
        self.assertTrue(response.json()["escalated"])
        inquiry = Inquiry.objects.get()
        self.assertEqual(self.client.session[assistant.INQUIRIES_KEY], [inquiry.pk])

    def test_bad_tool_requests(self):
        self.assertEqual(
            self.client.post(
                reverse("chatbot:live_tool"), "nope", content_type="application/json"
            ).status_code,
            400,
        )
        response = self.post_json("chatbot:live_tool", {"name": "drop_tables"})
        self.assertIn("error", response.json()["result"])

    def test_turns_are_saved_to_history_and_log(self):
        response = self.post_json(
            "chatbot:live_turn",
            {"message": "Any jazz?", "reply": "Yes, Friday!", "page": "/"},
        )
        self.assertEqual(response.status_code, 200)
        history = self.client.get(reverse("chatbot:history")).json()["messages"]
        self.assertEqual(
            history,
            [
                {"role": "user", "text": "Any jazz?"},
                {"role": "model", "text": "Yes, Friday!"},
            ],
        )
        log = ChatLog.objects.get()
        self.assertTrue(log.via_voice)
        self.assertEqual(log.outcome, ChatLog.Outcome.ANSWERED)

    def test_greeting_turns_have_no_visitor_message(self):
        self.post_json("chatbot:live_turn", {"reply": "Hi! How can I help?"})
        history = self.client.get(reverse("chatbot:history")).json()["messages"]
        self.assertEqual(history, [{"role": "model", "text": "Hi! How can I help?"}])

    def test_empty_turns_are_rejected(self):
        self.assertEqual(self.post_json("chatbot:live_turn", {}).status_code, 400)

    def test_call_traffic_has_its_own_rate_limit(self):
        for _ in range(views.RATE_LIMIT + 1):
            self.post_json("chatbot:live_turn", {"reply": "ok"})
        # Typed chat still works after a busy call.
        with mock.patch(GENERATE, return_value=text_response("Hello!")):
            self.assertEqual(self.post_message("Hi").status_code, 200)
        for _ in range(views.VOICE_CALL_RATE_LIMIT):
            self.post_json("chatbot:live_turn", {"reply": "ok"})
        response = self.post_json("chatbot:live_turn", {"reply": "ok"})
        self.assertEqual(response.status_code, 429)

    def test_widget_links_the_call_endpoints(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, 'data-live-url="/api/chat/live"')


@ENABLED
class SafetyAndFallbackTests(ChatTestCase):
    """Unsafe requests are declined; unknown answers offer human support."""

    def test_safety_blocked_replies_are_declined_not_escalated(self):
        blocked = {"candidates": [{"finishReason": "SAFETY", "content": {}}]}
        with mock.patch(GENERATE, return_value=blocked):
            response = self.post_message("Something inappropriate")
        self.assertEqual(response.json()["reply"], assistant.BLOCKED_REPLY)
        self.assertTrue(response.json()["declined"])
        self.assertFalse(Inquiry.objects.exists())
        self.assertEqual(ChatLog.objects.get().outcome, ChatLog.Outcome.DECLINED)

    @override_settings(CHATBOT_SUPPORT_EMAIL="help@example.com")
    def test_fallback_offers_the_support_email(self):
        with mock.patch(GENERATE, return_value=text_response("")):
            reply = self.post_message("Is there a coat check?").json()["reply"]
        self.assertIn("event manager", reply)
        self.assertIn("help@example.com", reply)
        page = self.client.get(reverse("login"))
        self.assertContains(page, "mailto:help@example.com")

    def test_prompt_includes_active_faqs_only(self):
        FAQ.objects.create(question="Hidden?", answer="Nope.", is_active=False)
        with mock.patch(GENERATE, return_value=text_response("Hi")) as generate:
            self.post_message("Can I get a refund?")
        prompt = generate.call_args.args[0]["systemInstruction"]["parts"][0]["text"]
        self.assertIn("Can I get a refund?", prompt)
        self.assertIn("[Cancellations & refunds]", prompt)
        self.assertNotIn("Hidden?", prompt)
        self.assertIn("Support email: none", prompt)

    def test_safety_settings_are_sent(self):
        with mock.patch("chatbot.gemini.urlopen") as urlopen:
            urlopen.side_effect = OSError("offline")
            self.post_message("Hi")
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(
            {s["category"] for s in sent["safetySettings"]},
            {
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            },
        )


@ENABLED
class ChatLogTests(ChatTestCase):
    """Conversations are logged anonymously, with personal details masked."""

    def test_logs_mask_emails_and_long_numbers(self):
        self.client.force_login(make_user("logger"))
        message = "Email me at jo@example.com or call (212) 555-0199 about 2026-11-07"
        with mock.patch(GENERATE, return_value=text_response("Sure!")):
            self.post_message(message, page="/search")
        log = ChatLog.objects.get()
        self.assertEqual(
            log.message, "Email me at [email] or call [number] about 2026-11-07"
        )
        self.assertEqual(
            (log.reply, log.outcome, log.page), ("Sure!", "answered", "/search")
        )
        self.assertTrue(log.signed_in)
        self.assertFalse(log.via_voice)
        self.assertEqual(str(log), log.message)
        self.assertNotIn("logger", str(vars(log)))

    def test_escalations_errors_and_voice_are_logged(self):
        with mock.patch(GENERATE, return_value=text_response("")):
            self.post_message("Unknown?")
        with mock.patch(GENERATE, side_effect=gemini.GeminiError("x")):
            self.post_message("Down?")
        with mock.patch(TRANSCRIBE, return_value=("Hola", "es")):
            with mock.patch(GENERATE, return_value=text_response("¡Hola!")):
                self.client.post(reverse("chatbot:voice"), {"audio": wav()})
        outcomes = list(
            ChatLog.objects.order_by("pk").values_list("outcome", "via_voice")
        )
        self.assertEqual(
            outcomes, [("escalated", False), ("error", False), ("answered", True)]
        )

    @override_settings(CHATBOT_LOG_CONVERSATIONS=False)
    def test_logging_can_be_turned_off(self):
        with mock.patch(GENERATE, return_value=text_response("Hi")):
            self.post_message("Hello")
        self.assertFalse(ChatLog.objects.exists())

    def test_logs_are_read_only_in_the_admin(self):
        admin = ChatLogAdmin(ChatLog, AdminSite())
        request = RequestFactory().get("/")
        self.assertFalse(admin.has_add_permission(request))
        self.assertFalse(admin.has_change_permission(request))
        self.assertEqual(str(FAQ(question="Q?")), "Q?")
