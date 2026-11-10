import logging
import re

from apps.core.client_pages import CAPABILITY_LETTERS

# /i/<token>/ (view), /p/<token>/ (pay), /k/<token>/ (key confirmation) and /l/<token>/ (payment link):
# the token is the only credential of a client page. The prefixes come from
# `client_pages.CAPABILITY_PREFIXES`, so a new kind of capability link is redacted as soon as it is listed.
_TOKEN_PATH = re.compile(rf"(/[{CAPABILITY_LETTERS}]/)[^/\s?#\"']+")


class RedactCapabilityTokens(logging.Filter):
    """Replace capability tokens in logged paths (django.request logs the path of every error)."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record is logged as it is
            return True
        redacted = _TOKEN_PATH.sub(r"\1<token>", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True
