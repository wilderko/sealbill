import logging
import re

from apps.core.client_pages import CAPABILITY_LETTERS

# /i/<token>/ (view), /p/<token>/ (pay), /k/<token>/ (key confirmation) and /l/<token>/ (payment link):
# the token is the only credential of a client page. The prefixes come from
# `client_pages.CAPABILITY_PREFIXES`, so a new kind of capability link is redacted as soon as it is listed.
_TOKEN_PATH = re.compile(rf"(/[{CAPABILITY_LETTERS}]/)[^/\s?#\"']+")


class RedactCapabilityTokens(logging.Filter):
    """Replace capability tokens in logged paths (django.request logs the path of every error), in the
    message, the traceback and the stack info: a 500 is logged with its traceback, whose exception text
    can carry the path. The traceback is formatted here once and stored as `exc_text`, which every
    formatter then prints as it is."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record is logged as it is
            return True
        redacted = _TOKEN_PATH.sub(r"\1<token>", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = _TOKEN_PATH.sub(r"\1<token>", record.exc_text)
        if record.stack_info:
            record.stack_info = _TOKEN_PATH.sub(r"\1<token>", record.stack_info)
        return True
