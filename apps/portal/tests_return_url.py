from django.utils import translation

from apps.invoices.models import Invoice
from apps.portal.tests import PortalBase


class BackLinkTests(PortalBase):
    def with_url(self, url="https://liberation.services/orders/9/?x=1", status=None):
        Invoice.objects.filter(pk=self.invoice.pk).update(return_url=url, **({"status": status} if status else {}))

    def test_pay_page_has_the_link_with_host_text_only(self):
        self.with_url()
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        self.assertContains(r, 'href="https://liberation.services/orders/9/?x=1"')
        self.assertContains(r, 'rel="noopener noreferrer"')
        self.assertContains(r, "Back to liberation.services")
        self.assertNotContains(r, "orders/9</")
        self.assertNotContains(r, "Back to liberation.services/orders")
        self.assertNotContains(r, 'class="btn btn--primary" href="https://liberation.services')

    def test_unpaid_is_not_primary_paid_is(self):
        self.with_url()
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        self.assertContains(r, "back-link")
        self.assertNotContains(r, "back-link btn--primary")
        self.with_url(status="paid")
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        self.assertContains(r, "btn btn--primary back-link")

    def test_no_return_url_no_link(self):
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        self.assertNotContains(r, "Back to")
        self.assertNotContains(r, "back-link")

    def test_view_page_has_it_too(self):
        self.with_url()
        self.assertContains(self.client.get(f"/i/{self.view}/?lang=en"), "Back to liberation.services")

    def test_slovak(self):
        self.with_url()
        self.assertContains(self.client.get(f"/p/{self.pay}/?lang=sk"), "Späť na liberation.services")

    def test_a_stored_unsafe_url_is_not_rendered(self):
        self.with_url("javascript:alert(1)")
        r = self.client.get(f"/p/{self.pay}/?lang=en")
        self.assertNotContains(r, "javascript:")
        self.assertNotContains(r, "Back to")


class VoidPageBackLinkTests(PortalBase):
    def test_void_page_keeps_the_back_link(self):
        from apps.invoices.services import void_invoice
        Invoice.objects.filter(pk=self.invoice.pk).update(return_url="https://liberation.services/orders/9/")
        void_invoice(Invoice.objects.get(pk=self.invoice.pk), "mistake", None)
        r = self.client.get(f"/i/{self.view}/?lang=en")
        self.assertContains(r, "Back to liberation.services")
        self.assertContains(r, "has been voided")
