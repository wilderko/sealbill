from django import forms
from django.utils.translation import gettext_lazy as _


class DeclarationForm(forms.Form):
    network = forms.ChoiceField(label=_("Network (optional)"), required=False)
    tx_hash = forms.CharField(label=_("Transaction number or hash (optional)"), max_length=128, required=False)
    note = forms.CharField(label=_("Note (optional)"), max_length=1000, required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, networks=(), **kwargs):
        super().__init__(*args, **kwargs)
        # The networks this invoice offers, then "Other" (a bank transfer, or a chain it does not list).
        self.fields["network"].choices = [("", _("Choose a network"))] + list(networks) + [("other", _("Other"))]
