"""Forms for answering questions forwarded by the assistant."""

from django import forms

from .models import Inquiry


class AnswerInquiryForm(forms.Form):
    """An event manager's answer to a visitor's question."""

    answer = forms.CharField(
        max_length=Inquiry._meta.get_field("answer").max_length,
        widget=forms.Textarea(attrs={"rows": 3}),
        strip=True,
    )
