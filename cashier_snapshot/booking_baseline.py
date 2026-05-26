"""Build the Beancount source baseline after booking but before transformations.

The standalone snapshot must distinguish changes performed by Beancount's core
booking step from changes performed by Python plugins.  Source syntax such as an
empty FIFO cost spec (``{}``) may be retained verbatim only when the fully
materialized entry is still identical to this pre-plugin booked baseline.
"""

from __future__ import annotations

from pathlib import Path

from beancount import loader
from beancount.core import data
from beancount.parser import booking


def load_booked_source_entries(root_path: Path) -> tuple[list, list, dict]:
    """Load entries through core booking, deliberately stopping before plugins.

    This mirrors the parse-and-book prefix of :func:`beancount.loader.load_file`.
    ``loader._parse_recursive`` is private upstream API, so its use is contained
    in this small adapter and covered by snapshot endpoint regressions.
    """
    entries, errors, options_map = loader._parse_recursive(
        [(str(root_path.resolve()), True)], None, None
    )
    entries.sort(key=data.entry_sortkey)
    entries, booking_errors = booking.book(entries, options_map)
    errors.extend(booking_errors)
    return entries, errors, options_map
