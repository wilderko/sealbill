# 5. Payment links

A **payment link** is one page, `/l/<token>/`, for a service at a set price (or an amount the buyer chooses), paid by
many people: a shop button, a QR code on a poster, a link in a newsletter. Open **Payment links** in the rail.

(These are not the *fiat links* of an organization from chapter 2, which only point to Revolut, PayPal and the like.)

![Payment links](../img/links.png)

## Create a link

**New payment link** asks for:

- **What is sold**: the organization that sells, the document (*Invoice* or *Payment request*), a title and description
  shown to buyers, and an optional picture (PNG, JPEG or WebP up to 1.5 MB, re-encoded, 960 × 540 suits the page).
- **Price**: currency, price and tax %, **Maximum quantity per order** (0 means unlimited, up to 200), or tick **The
  customer chooses the amount** with optional minimum and maximum.
- **What the buyer is asked**: the e-mail address and "Are you a US tax resident?" are **always** asked; the name, a
  billing address, a shipping address and a phone number are off, optional or required; up to five extra questions.
- **Payment methods**: the issuer's default methods, or a **custom set** such as "USDC on Arbitrum One", "Bank transfer
  in EUR" or "Bitcoin over Lightning". A custom set names kinds of payment, not accounts, so the US-resident issuer
  offers its own accounts of the same kinds.
- **Availability**: active, *Valid until*, *Total uses*, **Paid orders need fulfilling** and **E-mail staff for each paid
  order**, and an internal note that only staff see.

![The new link form](../img/link-new.png)

The link works as soon as it is saved and active.

## Share it

The link's page shows the URL with **Copy**, a **QR code** (PNG or SVG), a **printable sheet** (PDF), the HTML of a
**button for a web page**, and **Replace the URL**, which gives a new address and stops the old one at once. Anyone with
the URL can buy; add `?promoCode=CODE` to fill in a coupon.

![A link, with its QR code, coupons and orders](../img/link-detail.png)

**Coupons**: a code, a percentage off the line (before tax, never a free order), optionally a number of uses and an
end date. **Copy link with code** gives a URL that fills it in.

## What the buyer sees

![The public page of a link](../img/link-public.png)

The buyer types an e-mail address, a name if asked, answers the US question and continues to payment. The page they
land on is the ordinary **pay page** of chapter 4.

![The pay page of an order](../img/link-checkout-pay.png)

- Each checkout makes an **order**: an invoice or a payment request with one line, numbered and sent at once, with a
  frozen snapshot. **No e-mail is sent before payment**; the pay page says to keep the page. The first letter is the
  receipt, or the partial-payment letter, and it carries the order's links.
- **US tax residents.** The answer typed by the buyer decides which organization issues the order: a yes to the
  US-resident issuer, a no to the link's own. What a contact stores never decides it, so a public link reveals nothing
  that is stored. A typed answer that the stored flag contradicts becomes an item in Needs a person, and for a "no"
  against a stored "yes" the order is not offered for fulfilment until a person has checked with the buyer.
- **A public form never shows or changes a contact.** An address that matches a client of record books the order to that
  client. Otherwise a *link buyer* is made from the typed data. Link buyers are listed in Contacts with a filter, are
  found by no lookup and reused by nothing; a person can make one a client of record from its page, once checked.

  ![Contacts, link buyers only](../img/contacts-link-buyers.png)

## Orders, uses and limits

- A checkout **holds** one of the link's uses (and the coupon's) for 60 minutes; paying turns the hold into a use. A
  payment that arrives after the hold lapsed keeps its price; if that takes a capped link or coupon over its cap, staff
  get one item in Needs a person.
- **Unpaid orders are voided after 7 days.** Money that arrives later goes to review, as for any void invoice.
- **Fulfilment.** With *Paid orders need fulfilling* on, paid orders are listed on the link page as to fulfil until
  someone presses **Mark fulfilled**.
- **Limits**: 5 checkout attempts per 10 minutes per address, and 50 completed checkouts per hour per link. No page may
  frame a link.
- Creating or editing a link through the API is `invoices:write`; the link's URL is handed out on create, on rotate and
  when one link is read.
