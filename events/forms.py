"""Forms for hosted event listings, event discussions, and contact requests."""

from django import forms
from django.utils import timezone

from .filters import CATEGORY_GROUPS, CATEGORY_SEPARATOR, NYC_TIMEZONE
from .models import ContactRequest, Event, EventPost, EventReply

DATETIME_INPUT_FORMAT = "%Y-%m-%dT%H:%M"
CATEGORY_LABELS = {slug: label for slug, label, _ in CATEGORY_GROUPS}


class NYCDateTimeField(forms.DateTimeField):
    """Read and show times as New York wall-clock time, where events happen."""

    input_formats = [DATETIME_INPUT_FORMAT]
    widget = forms.DateTimeInput(
        attrs={"type": "datetime-local"}, format=DATETIME_INPUT_FORMAT
    )

    def to_python(self, value):
        """Interpret the submitted time in New York instead of the server zone."""
        with timezone.override(NYC_TIMEZONE):
            return super().to_python(value)

    def prepare_value(self, value):
        """Show stored times in New York time when the form is displayed."""
        with timezone.override(NYC_TIMEZONE):
            return super().prepare_value(value)


class EventListingForm(forms.ModelForm):
    """Validate the details an event holder fills in to post or edit an event.

    Categories are chosen from the same groups as the filter bar and stored
    as their labels, so hosted events show up under those filters.
    """

    start_time = NYCDateTimeField(label="Start date and time")
    end_time = NYCDateTimeField(
        label="End date and time",
        required=False,
        help_text="Must be after the start.",
    )
    categories = forms.MultipleChoiceField(
        label="Categories",
        choices=[(slug, label) for slug, label, _ in CATEGORY_GROUPS],
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": "Choose at least one category."},
    )

    class Meta:
        model = Event
        fields = (
            "title",
            "description",
            "start_time",
            "end_time",
            "location",
            "borough",
            "categories",
            "image_url",
            "registration_url",
        )
        labels = {
            "location": "Location or address",
            "image_url": "Image link",
            "registration_url": "Ticketing or registration link",
        }
        help_texts = {
            "image_url": "A link to a picture of your event.",
            "registration_url": "Where people can buy tickets or sign up.",
        }
        widgets = {"description": forms.Textarea(attrs={"rows": 6})}

    def __init__(self, *args, **kwargs):
        """Require the details every listing needs, and show saved categories."""
        super().__init__(*args, **kwargs)
        for name in ("description", "location", "borough"):
            self.fields[name].required = True
        self.fields["borough"].choices = [("", "Choose a borough")] + list(
            Event.Borough.choices
        )
        saved = self.instance.categories.split(CATEGORY_SEPARATOR)
        self.initial["categories"] = [
            slug for slug, label in CATEGORY_LABELS.items() if label in saved
        ]

    def clean_categories(self):
        """Store the chosen groups as one string, in the filter bar's order."""
        chosen = self.cleaned_data["categories"]
        return CATEGORY_SEPARATOR.join(
            label for slug, label in CATEGORY_LABELS.items() if slug in chosen
        )

    def clean(self):
        """Reject new start times in the past and events that end too early.

        An unchanged start time is allowed to be in the past, so hosts can
        still fix the details of an event that is already under way.
        """
        cleaned = super().clean()
        start, end = cleaned.get("start_time"), cleaned.get("end_time")
        if start and "start_time" in self.changed_data and start <= timezone.now():
            self.add_error("start_time", "Choose a start time in the future.")
        if start and end and end <= start:
            self.add_error("end_time", "The end time must be after the start time.")
        return cleaned


class NotifyAttendeesForm(forms.Form):
    """Validate a host's message to everyone interested in their event."""

    subject = forms.CharField(label="Subject", max_length=200)
    message = forms.CharField(
        label="Message", max_length=5000, widget=forms.Textarea(attrs={"rows": 4})
    )


class CancelEventForm(forms.Form):
    """Validate the optional note a host sends when cancelling an event."""

    message = forms.CharField(
        label="Message to attendees",
        max_length=2000,
        required=False,
        help_text="Included in the cancellation email and notification.",
        widget=forms.Textarea(
            attrs={"rows": 4, "placeholder": "Let people know why, or what's next."}
        ),
    )


class EventPostForm(forms.ModelForm):
    """Validate a new discussion post."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["body"].strip = False

    class Meta:
        model = EventPost
        fields = ("body",)
        widgets = {
            "body": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "Share your thoughts about this event...",
                }
            ),
        }
        labels = {"body": "Your post"}

    def clean_body(self):
        """Reject posts containing only whitespace."""
        body = self.cleaned_data["body"].strip()
        if not body:
            raise forms.ValidationError("Your post cannot be empty.")
        return body


class EventReplyForm(forms.ModelForm):
    """Validate a reply to an event discussion post."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["body"].strip = False

    class Meta:
        model = EventReply
        fields = ("body",)
        widgets = {
            "body": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "Write a reply...",
                }
            ),
        }
        labels = {"body": "Your reply"}

    def clean_body(self):
        """Reject replies containing only whitespace."""
        body = self.cleaned_data["body"].strip()
        if not body:
            raise forms.ValidationError("Your reply cannot be empty.")
        return body


class ContactRequestForm(forms.ModelForm):
    """Validate an optional message accompanying a contact request."""

    class Meta:
        model = ContactRequest
        fields = ("message",)
        widgets = {
            "message": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "Introduce yourself (optional)...",
                }
            ),
        }
        labels = {"message": "Message (optional)"}


class ContactStartForm(forms.Form):
    """Choose a shared upcoming event and optionally introduce yourself."""

    event = forms.ModelChoiceField(
        queryset=Event.objects.none(),
        label="Shared event",
    )
    message = forms.CharField(
        label="Introduction (optional)",
        required=False,
        max_length=500,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "Introduce yourself (optional)...",
            }
        ),
    )

    def __init__(self, *args, shared_events, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event"].queryset = shared_events

    def clean_message(self):
        return self.cleaned_data["message"].strip()


class DirectMessageForm(forms.Form):
    """Compose a private message."""

    body = forms.CharField(
        label="Message",
        max_length=5000,
        strip=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": "Write a message...",
                "aria-label": "Write a message",
            }
        ),
    )

    def clean_body(self):
        body = self.cleaned_data["body"].strip()
        if not body:
            raise forms.ValidationError("Your message cannot be empty.")
        return body
