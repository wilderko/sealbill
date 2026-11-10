"""Staff alerts go out on two channels, e-mail and the `staff.alert` webhook event, and each keeps
its own bookkeeping (a mark per row and channel), so one channel failing never silences the other:
an SMTP outage still emits the event once per episode, and a lost event does not resend the e-mail.

Every alert source works the same way: it lists the rows not yet announced on each channel, tries
each channel with its own pending rows only, and asks `marks` which rows each channel may now mark.

- A channel that told someone marks the rows it announced.
- A channel with nobody to tell (STAFF_NOTIFY_EMAILS empty; no endpoint wanting staff alerts) marks a
  row once the other channel has told someone about it, so a channel configured later is not handed
  old news. With nobody to tell on either, nothing is marked: whoever is configured next hears of it.
- A channel that failed (SMTP refused, the event could not be recorded) marks nothing and is tried
  again on the next run.
"""
TOLD, NOBODY, FAILED = "told", "nobody", "failed"


def webhook(data: dict) -> str:
    """The alert as a `staff.alert` event about the service itself (`data`: kinds, names and counts,
    never client data). TOLD, NOBODY or FAILED; nothing is recorded when nobody wants it."""
    from apps.webhooks import events

    return events.staff_alert_outcome(data)


def marks(rows, email: str | None, webhook: str | None) -> tuple[list, list]:
    """`rows`: (key, already announced by e-mail, already announced as an event) for every row that
    is pending on at least one channel. `email` and `webhook`: each channel's outcome this run (None
    when it had nothing pending). Returns the keys to mark for e-mail and for the event."""
    for_email, for_webhook = [], []
    for key, email_done, webhook_done in rows:
        told_by_email = email_done or email == TOLD
        told_by_webhook = webhook_done or webhook == TOLD
        if not email_done and (email == TOLD or (email == NOBODY and told_by_webhook)):
            for_email.append(key)
        if not webhook_done and (webhook == TOLD or (webhook == NOBODY and told_by_email)):
            for_webhook.append(key)
    return for_email, for_webhook
