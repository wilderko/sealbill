from django.urls import path, re_path, register_converter
from rest_framework.permissions import AllowAny

from . import views as v

class PkConverter:
    """A database id; longer digit runs cannot exist and would overflow the driver."""

    regex = "[0-9]{1,18}"

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return str(value)


register_converter(PkConverter, "pk")

public = {"authentication_classes": [], "permission_classes": [AllowAny]}

urlpatterns = [
    path("api/v1/me", v.Me.as_view()),
    path("api/v1/contacts", v.ContactList.as_view()),
    path("api/v1/contacts/<pk:pk>", v.ContactDetail.as_view()),
    path("api/v1/contacts/<pk:pk>/keys", v.ContactKeys.as_view()),
    path("api/v1/keys/<pk:pk>/revoke", v.KeyRevoke.as_view()),
    path("api/v1/invoices", v.InvoiceList.as_view()),
    path("api/v1/invoices/<pk:pk>", v.InvoiceDetail.as_view()),
    path("api/v1/invoices/<pk:pk>/send", v.InvoiceSend.as_view()),
    path("api/v1/invoices/<pk:pk>/reissue-links", v.InvoiceReissueLinks.as_view()),
    path("api/v1/invoices/<pk:pk>/void", v.InvoiceVoid.as_view()),
    path("api/v1/invoices/<pk:pk>/pdf", v.InvoicePdf.as_view()),
    path("api/v1/invoices/<pk:pk>/payments", v.InvoicePayments.as_view()),
    path("api/v1/invoices/<pk:pk>/quotes", v.InvoiceQuotes.as_view()),
    path("api/v1/quotes/<pk:pk>", v.QuoteDetail.as_view()),
    path("api/v1/payment-links", v.PaymentLinkList.as_view()),
    path("api/v1/payment-links/<pk:pk>", v.PaymentLinkDetail.as_view()),
    path("api/v1/payment-links/<pk:pk>/rotate", v.PaymentLinkRotate.as_view()),
    path("api/v1/payment-methods", v.PaymentMethodList.as_view()),
    path("api/v1/payments", v.PaymentList.as_view()),
    path("api/v1/payments/<pk:pk>/confirm", v.PaymentConfirm.as_view()),
    path("api/v1/payments/<pk:pk>/reject", v.PaymentReject.as_view()),
    path("api/v1/payments/<pk:pk>/reverse", v.PaymentReverse.as_view()),
    path("api/v1/payments/<pk:pk>/assign", v.PaymentAssign.as_view()),
    path("api/v1/payments/<pk:pk>/acknowledge-refund", v.PaymentAcknowledgeRefund.as_view()),
    path("api/v1/exports/invoices.csv", v.InvoiceExport.as_view()),
    path("api/v1/exports/payments.csv", v.PaymentExport.as_view()),
    path("api/v1/declarations", v.DeclarationList.as_view()),
    path("api/v1/declarations/<pk:pk>/dismiss", v.DeclarationDismiss.as_view()),
    path("api/v1/webhooks", v.WebhookList.as_view()),
    path("api/v1/webhooks/<pk:pk>", v.WebhookDetail.as_view()),
    path("api/v1/webhooks/<pk:pk>/deliveries", v.WebhookDeliveries.as_view()),
    path("api/v1/webhooks/<pk:pk>/ping", v.WebhookPing.as_view()),
    path("api/v1/schema", v.ThrottledSchemaView.as_view(**public), name="api-schema"),
    path("api/v1/docs", v.ThrottledDocsView.as_view(url_name="api-schema", **public), name="api-docs"),
    path("llms.txt", v.llms_txt, name="llms-txt"),
    re_path(r"^api/v1(?:/.*)?$", v.not_found),  # last: everything else under /api/v1 is a JSON 404
]
