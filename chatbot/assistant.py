"""Run one turn of the conversation between a visitor and the assistant."""

from django.conf import settings
from django.urls import Resolver404, resolve

from events.models import Event

from . import gemini
from .prompts import system_prompt
from .tools import Toolbox

HISTORY_KEY = "chatbot_history"
INQUIRIES_KEY = "chatbot_inquiries"
# Earlier messages sent back to Gemini for context, and kept in the session.
HISTORY_LIMIT = 20
# Rounds of tool calls allowed before the assistant must answer.
MAX_TOOL_ROUNDS = 5

BLOCKED_REPLY = (
    "Sorry, I can't help with that request. I'm happy to help with events, "
    "bookings, or your account."
)
ESCALATED_REPLY = (
    "I'm not sure about that, so I've passed your question to the event "
    "manager. I'll let you know here as soon as they reply."
)


VOICE_INSTRUCTIONS = """
YOU ARE ON A LIVE VOICE CALL
The visitor is talking to you out loud, and everything you say is spoken.
- Speak like a warm, friendly American woman chatting naturally with a
  friend: relaxed, conversational American English, with contractions.
  React naturally ("Oh, nice!", "Sure thing", "Hmm, let me check").
- Keep your voice soft, gentle, and calm, at an easy, unhurried pace.
  Never sound loud, sharp, or overly excited.
- The visitor speaks first. When they greet you, greet them back briefly
  and ask how you can help.
- Reply in the visitor's language. When it is English, use an American
  accent and American wording.
- Keep each turn short: one to three sentences. Offer to tell more rather
  than reading long lists; mention two or three events at most at a time.
- Never use Markdown, bullets, emoji, or read out links, URLs, or event ids.
  Name the page instead, e.g. "the event's page" or "Account settings".
- Say dates and times naturally, e.g. "this Saturday at ten in the morning".
- Before looking something up, you may say a quick "Let me check."
- If the visitor interrupts, stop and answer what they just said.
"""
# Earlier text-chat messages repeated in a voice call's instructions.
VOICE_HISTORY_LIMIT = 10
VOICE_HISTORY_CHARS = 600


def escalated_reply():
    """Return the fallback reply, offering the support email when set."""
    if settings.CHATBOT_SUPPORT_EMAIL:
        return (
            f"{ESCALATED_REPLY} You can also email "
            f"{settings.CHATBOT_SUPPORT_EMAIL}."
        )
    return ESCALATED_REPLY


def event_on_page(path):
    """Return the event whose detail page is at ``path``, if it is one."""
    try:
        match = resolve(str(path or "").split("?")[0])
    except Resolver404:
        return None
    if match.url_name != "event_detail":
        return None
    return Event.objects.filter(pk=match.kwargs["pk"]).first()


def get_history(session):
    """Return the stored conversation as a list of role/text dicts."""
    history = session.get(HISTORY_KEY)
    return history if isinstance(history, list) else []


def append_history(session, *messages):
    """Store messages, keeping only the most recent ones."""
    session[HISTORY_KEY] = (get_history(session) + list(messages))[-HISTORY_LIMIT:]


def reply(request, message, page=""):
    """Answer a visitor's message, using tools to look things up.

    Returns:
        dict: ``reply`` text, whether the question was ``escalated`` to the
        event manager, and whether Gemini ``declined`` it for safety.

    Raises:
        gemini.GeminiError: when Gemini cannot be reached.
    """
    session = request.session
    contents = [
        {"role": item["role"], "parts": [{"text": item["text"]}]}
        for item in get_history(session)
        if item.get("role") in ("user", "model") and item.get("text")
    ]
    contents.append({"role": "user", "parts": [{"text": message}]})
    current_event = event_on_page(page)
    toolbox = Toolbox(request.user, list(session.get(INQUIRIES_KEY, [])))
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_prompt(request.user, current_event)}]
        },
        "contents": contents,
        "tools": [{"functionDeclarations": toolbox.declarations}],
    }

    text = ""
    declined = False
    # Shared across rounds so a turn that falls back stays on one model, whose
    # thought signatures it sends back.
    models = gemini.model_chain()
    for _ in range(MAX_TOOL_ROUNDS):
        content = gemini.first_content(gemini.generate_content(payload, models))
        if content is None:
            text = BLOCKED_REPLY
            declined = True
            break
        calls = [
            part["functionCall"]
            for part in content.get("parts") or []
            if "functionCall" in part
        ]
        if not calls:
            text = gemini.response_text(content)
            break
        # Send the model's turn back unchanged (it may carry thought
        # signatures), followed by the result of every requested call.
        contents.append(content)
        contents.append(
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": call.get("name", ""),
                            "response": {
                                "result": toolbox.call(
                                    call.get("name", ""), call.get("args")
                                )
                            },
                        }
                    }
                    for call in calls
                ],
            }
        )

    if not text:
        # The model ran out of rounds or said nothing: hand the question to a
        # person rather than leave the visitor without an answer.
        toolbox.escalate_to_event_manager(
            question=message, event_id=current_event.pk if current_event else None
        )
        text = escalated_reply()

    session[INQUIRIES_KEY] = toolbox.inquiry_ids[-HISTORY_LIMIT:]
    append_history(
        session,
        {"role": "user", "text": message},
        {"role": "model", "text": text},
    )
    return {"reply": text, "escalated": toolbox.escalated, "declined": declined}


def earlier_conversation(session):
    """Return recent chat messages as text a voice call can pick up from."""
    lines = []
    for item in get_history(session)[-VOICE_HISTORY_LIMIT:]:
        if item.get("role") in ("user", "model") and item.get("text"):
            who = "Visitor" if item["role"] == "user" else "You"
            lines.append(f"{who}: {item['text'][:VOICE_HISTORY_CHARS]}")
    if not lines:
        return ""
    return (
        "\nEARLIER IN THIS CONVERSATION (continue from it; don't repeat it)\n"
        + "\n".join(lines)
        + "\n"
    )


def live_setup(request, page=""):
    """Build the Live API setup for a voice call with this visitor.

    It carries the same instructions and tools as typed chat, plus guidance
    for speaking, and asks Gemini to transcribe both sides as they talk.
    """
    instructions = (
        system_prompt(request.user, event_on_page(page))
        + VOICE_INSTRUCTIONS
        + earlier_conversation(request.session)
    )
    return {
        "model": f"models/{settings.GEMINI_LIVE_MODEL}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "languageCode": settings.GEMINI_LIVE_LANGUAGE,
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": settings.GEMINI_LIVE_VOICE}
                },
            },
        },
        "systemInstruction": {"parts": [{"text": instructions}]},
        "tools": [{"functionDeclarations": Toolbox.declarations}],
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
    }


def run_live_tool(request, name, args):
    """Run one tool the voice assistant asked for, as this visitor.

    Returns:
        tuple: the tool's result and whether it escalated the question.
    """
    session = request.session
    toolbox = Toolbox(request.user, list(session.get(INQUIRIES_KEY, [])))
    result = toolbox.call(name, args)
    if toolbox.escalated:
        session[INQUIRIES_KEY] = toolbox.inquiry_ids[-HISTORY_LIMIT:]
    return result, toolbox.escalated


def save_voice_turn(session, message, text):
    """Keep a spoken exchange in the history so typed chat can continue it."""
    turn = []
    if message:
        turn.append({"role": "user", "text": message})
    if text:
        turn.append({"role": "model", "text": text})
    append_history(session, *turn)
