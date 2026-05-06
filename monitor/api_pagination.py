"""
monitor/api_pagination.py

Cursor-based pagination helper for the Read API.

Cursor encodes the last seen value of the queryset's ordering field as
base64(str(value)). Works with UUID, int, and timestamp ordering fields.
"""
import base64
import binascii
from typing import Any


DEFAULT_LIMIT = 50
MAX_LIMIT = 500


class CursorError(ValueError):
    """Raised when the client sends an unparsable cursor."""


def _encode(value: Any) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).rstrip(b"=").decode()


def _decode(cursor: str) -> str:
    # Re-pad b64 (we strip = on encode)
    pad = "=" * (-len(cursor) % 4)
    try:
        return base64.urlsafe_b64decode((cursor + pad).encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise CursorError(f"Invalid cursor: {exc}")


def get_limit(request, default: int = DEFAULT_LIMIT, max_limit: int = MAX_LIMIT) -> int:
    raw = request.GET.get("limit")
    if raw is None:
        return default
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(max_limit, limit))


def paginate(qs, request, default: int = DEFAULT_LIMIT, max_limit: int = MAX_LIMIT):
    """
    Paginate *qs* using cursor pagination based on the queryset's first ordering field.

    Returns: (items: list, next_cursor: str | None, limit: int)

    Raises CursorError if the cursor is malformed.
    """
    limit = get_limit(request, default=default, max_limit=max_limit)

    # Resolve ordering — explicit on the queryset, then model meta, then 'pk'.
    order = list(qs.query.order_by) or list(qs.model._meta.ordering) or ["pk"]
    primary = order[0]
    descending = primary.startswith("-")
    field = primary.lstrip("-")

    cursor = request.GET.get("cursor")
    if cursor:
        cursor_value = _decode(cursor)
        lookup = f"{field}__{'lt' if descending else 'gt'}"
        qs = qs.filter(**{lookup: cursor_value})

    items = list(qs[: limit + 1])
    has_more = len(items) > limit
    items = items[:limit]

    next_cursor = None
    if has_more and items:
        last_value = getattr(items[-1], field, None)
        if field == "pk":
            last_value = items[-1].pk
        if last_value is not None:
            next_cursor = _encode(last_value)

    return items, next_cursor, limit
