# 6. Needs a person

**Needs a person** is the first page and the one to keep an eye on. It lists what no program should decide on its own.
The rail shows how many items are open; with nothing to do the page says so.

![The queues with something in them](../img/needs-person.png)

The queues are shown only when they have items; **Show N queues** lists the empty ones too, so you can see what the page
watches.

![All the queues](../img/needs-person-queues.png)

| Queue | What it is and what you do |
|---|---|
| **Declared payments** | A client pressed "I have paid" on an invoice. Check your bank or the chain. Recording the payment on the invoice settles the claim; **Dismiss declaration** on the invoice closes a false one. |
| **Payments to review** | Money that matched no invoice or matched badly, with the reason: the token is not accepted, no open invoice matches, more than one could match, it came after the quote expired, a duplicate, far above what was owed, the invoice was already closed, a very small "dust" transfer (probably address-poisoning spam, folded away), a dropped transaction, or a BTCPay report of a partial, late, invalid or hand-marked invoice. **Confirm** it onto its invoice, **Reject** it with a note, or **Assign to an invoice** (invoice number, amount and a note). A chain transfer can be acted on once it is final. |
| **Waiting for staff confirmation** | Monero and Zcash payments (through BTCPay) and money recorded by hand: nothing watches for these, so check the wallet or the bank, then **Confirm** or **Reject**. Only a person may confirm; no API token can. |
| **Gateway payments seen but not settled** | BTCPay or Lightning payments the gateway saw more than six hours ago and has not settled. |
| **Refunds due** | A crypto payment above its quote. Send the difference back to the address shown, then **Refunded** with a note. |
| **Confirmed payments the gateway now calls invalid** | BTCPay settled it, then called it invalid (a double spend, a reorg). Nothing is undone by code: reverse the payment from its invoice, or keep it. |
| **Exchange rates refused** | A fiat leg of the rates is stale or jumped. Check the ECB's rate; **accept** a jump that is real. Crypto quotes in that currency are refused meanwhile. |
| **Watchers that could not look** | A chain or gateway reader has been failing for ten minutes or more (the staff were e-mailed too). |
| **Transfers seen but not recorded** | A transfer a watcher saw could not be recorded yet; it is retried on every pass. |
| **Blocks not scanned, or to check** | A stretch of blocks was skipped. Check it by hand, then **Acknowledge** with what you checked. |
| **Overpaid invoices** | Refund outside the system or **Keep the overpayment** (chapter 4). |
| **US tax residents with invoices from another issuer** | Void the invoice and create it again; the US-resident organization then issues it. |
| **Encryption keys that stopped working** / **expiring within 30 days** | Chapter 3. |
| **E-mail not delivered** | A message that failed for about sixteen hours. **Retry** it, or **Dismiss** it with a reason (it is then cancelled for good). |
| **Payment links** | A paid order took a capped link past its cap, the issuer offers none of a link's methods, a typed US answer contradicts the stored one, or no organization is the US-resident issuer. Press **Resolved** with what you checked. |
| **Webhooks not delivered** | A delivery that failed after its retries. **Retry** or **Dismiss** with a reason. |

Two rules run through the page:

- A payment that is **waiting for a person** is confirmed or assigned only by a signed-in staff member. A token, even an
  `admin` one, is answered `409 needs_person` and nothing changes. Recording a manual payment is a person's job too.
- Every action asks for a note where it matters, and every action is in the audit log.

The same queues can be worked through the API with `payments:confirm`, except the person-only ones (chapter 9).
The two lists you will use to find out *why* something is stuck are **Settings > E-mail outbox** (every letter, with its
state, attempts and the reason it failed; recipients and contents are never shown) and **Settings > Webhook deliveries**.

![The e-mail outbox](../img/outbox.png)

![Webhook deliveries](../img/deliveries.png)
