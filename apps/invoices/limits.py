"""How big an invoice may be, and which issue dates it may carry (audit API-5, API-9).

One place for the numbers, read by the API serializer, the staff form and the services
(`create_invoice`, `update_draft`), so a request refused at one door is refused at every door.
Rendering a PDF takes time in proportion to its lines (about 7 ms each) and runs in the single
worker when the letter goes out, so the line count is what bounds that time.
"""
import datetime

from django.utils import timezone

from apps.invoices.errors import InvoiceError

MAX_LINES = 200
MAX_TITLE = 2000
MAX_NOTES = 5000
MAX_LINE_NAME = 500  # LineItem.name's own max_length
MAX_LINE_DESCRIPTION = 2000
# The staff form holds fewer: Django refuses a POST of more than DATA_UPLOAD_MAX_NUMBER_FIELDS
# (1000) fields, and every line is six of them.
STAFF_FORM_LINES = 100

EARLIEST_ISSUE_DATE = datetime.date(2000, 1, 1)
ISSUE_DATE_DAYS_AHEAD = 365


def latest_issue_date() -> datetime.date:
    return timezone.localdate() + datetime.timedelta(days=ISSUE_DATE_DAYS_AHEAD)


def issue_date_error(day: datetime.date) -> str | None:
    """Why `day` cannot be an invoice's issue date, or None when it can."""
    if day < EARLIEST_ISSUE_DATE or day > latest_issue_date():
        return (f"the issue date must be between {EARLIEST_ISSUE_DATE.isoformat()} and "
                f"{ISSUE_DATE_DAYS_AHEAD} days from today")
    return None


def due_date(issue_date: datetime.date, due_days) -> datetime.date | None:
    """`issue_date` plus `due_days` (None: due on receipt); an out-of-range sum is refused, never raised."""
    if due_days is None:
        return None
    try:
        return issue_date + datetime.timedelta(days=due_days)
    except (OverflowError, ValueError):
        raise InvoiceError("the due date is out of range") from None


def check_issue_date(day: datetime.date) -> None:
    error = issue_date_error(day)
    if error:
        raise InvoiceError(error)


def check_content(lines, title, notes) -> None:
    """InvoiceError when the invoice holds more lines, or longer texts, than the caps allow."""
    if len(lines) > MAX_LINES:
        raise InvoiceError(f"an invoice may have at most {MAX_LINES} lines")
    if len(title or "") > MAX_TITLE:
        raise InvoiceError(f"the title may have at most {MAX_TITLE} characters")
    if len(notes or "") > MAX_NOTES:
        raise InvoiceError(f"the notes may have at most {MAX_NOTES} characters")
    for line in lines:
        if len(line.get("name") or "") > MAX_LINE_NAME:
            raise InvoiceError(f"a line name may have at most {MAX_LINE_NAME} characters")
        if len(line.get("description") or "") > MAX_LINE_DESCRIPTION:
            raise InvoiceError(f"a line description may have at most {MAX_LINE_DESCRIPTION} characters")
