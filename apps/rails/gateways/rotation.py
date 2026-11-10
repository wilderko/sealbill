"""Where a poll pass starts.

A pass over one account stops after three failed lookups in a row, so that a node
that is really down is not hammered. Quotes come in a fixed order, so three lookups
that always fail at the front would hide every later quote for good, and a paid
invoice with them. Starting somewhere else every period gets past them.
"""


def rotated(items, period_seconds, now):
    """`items` starting at an offset that moves one step per period and wraps around.

    Stateless: the clock is the only thing it needs. `period_seconds` is the job's
    own period, so consecutive passes start one item further on.
    """
    if len(items) < 2:
        return items
    offset = int(now.timestamp() // period_seconds) % len(items)
    return items[offset:] + items[:offset]
