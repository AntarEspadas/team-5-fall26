"""Forms for event discussions and contact requests."""

from django import forms

from .models import ContactRequest, EventPost, EventReply


class EventPostForm(forms.ModelForm):
    """Validate a new discussion post."""

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
