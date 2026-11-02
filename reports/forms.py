"""Forms for submitting reports."""

from django import forms

from .models import Report


class EventReportForm(forms.ModelForm):
    """Accept a reason and optional context, never the reporter or target."""

    class Meta:
        model = Report
        fields = ("reason", "details")
        labels = {"details": "Additional details"}
        help_texts = {"details": "Add any information that explains your concern."}
        widgets = {"details": forms.Textarea(attrs={"rows": 5})}
        error_messages = {"reason": {"required": "Choose a reason for your report."}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].choices = [("", "Select a reason")] + list(
            Report.Reason.choices
        )
