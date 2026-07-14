from __future__ import annotations

import json
from typing import Any


class DuplicateJSONKeyError(json.JSONDecodeError):
    pass


def is_utf8_encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def strict_json_loads(value: str) -> Any:
    """Decode JSON while refusing duplicate object keys."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, item in pairs:
            if key in parsed:
                raise DuplicateJSONKeyError(f"duplicate JSON key: {key}", value, 0)
            parsed[key] = item
        return parsed

    def reject_constant(constant: str) -> Any:
        raise json.JSONDecodeError(f"invalid JSON constant: {constant}", value, 0)

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
