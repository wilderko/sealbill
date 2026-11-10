"""One place that decides which language a contact is addressed in."""


def activate_for(contact) -> str:
    """Language code for pages, PDFs and e-mails sent to `contact`.

    `Contact.language` already falls back from the contact's own choice to the
    organization's default and finally to English.
    """
    return contact.language
