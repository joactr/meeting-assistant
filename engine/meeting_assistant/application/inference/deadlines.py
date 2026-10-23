"""Deterministic, disclosed EN/ES deadline interpretation at meeting-local date."""

import re
from datetime import date, timedelta
from typing import Any, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = {
    "monday": 0,
    "lunes": 0,
    "tuesday": 1,
    "martes": 1,
    "wednesday": 2,
    "miércoles": 2,
    "miercoles": 2,
    "thursday": 3,
    "jueves": 3,
    "friday": 4,
    "viernes": 4,
    "saturday": 5,
    "sábado": 5,
    "sabado": 5,
    "sunday": 6,
    "domingo": 6,
}


class DeadlineInterpretation(TypedDict):
    due_date: str | None
    rule: str | None
    explanation: str


def interpret_deadline(text: str | None, context: dict[str, Any]) -> DeadlineInterpretation:
    if not text:
        return {"due_date": None, "rule": None, "explanation": "No stated deadline"}
    normalized = " ".join(text.casefold().split())
    explicit = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", normalized)
    if explicit:
        try:
            return {
                "due_date": date.fromisoformat(explicit[1]).isoformat(),
                "rule": "explicit-iso-date-v1",
                "explanation": "Explicit calendar date retained from deadline wording",
            }
        except ValueError:
            return {
                "due_date": None,
                "rule": None,
                "explanation": "Deadline contains an invalid calendar date",
            }
    local = context.get("meeting_local_date")
    timezone = context.get("meeting_timezone")
    if not local or not timezone:
        return {
            "due_date": None,
            "rule": None,
            "explanation": "Meeting-local date/timezone unavailable; original deadline wording retained",
        }
    try:
        base = date.fromisoformat(local)
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return {
            "due_date": None,
            "rule": None,
            "explanation": "Meeting date/timezone is invalid; original wording retained",
        }
    offset = None
    rule = None
    if re.search(r"\b(today|hoy)\b", normalized):
        offset, rule = 0, "today-en-es-v1"
    elif re.search(r"\b(tomorrow|mañana)\b", normalized):
        offset, rule = 1, "tomorrow-en-es-v1"
    else:
        number = re.search(r"\b(?:in|en)\s+(\d{1,3})\s+(?:days?|días?|dias?)\b", normalized)
        if number:
            offset, rule = int(number[1]), "relative-days-en-es-v1"
        else:
            for weekday, index in WEEKDAYS.items():
                if re.search(r"\b" + weekday + r"\b", normalized):
                    offset = (index - base.weekday()) % 7
                    if offset == 0:
                        offset = 7
                    rule = "next-occurrence-weekday-en-es-v1"
                    break
    if offset is None:
        return {
            "due_date": None,
            "rule": None,
            "explanation": "Deadline wording is not resolved by the versioned deterministic date rules",
        }
    resolved = (base + timedelta(days=offset)).isoformat()
    return {
        "due_date": resolved,
        "rule": rule,
        "explanation": f"Interpreted against meeting local date {base.isoformat()} in {timezone}; not the rerun date",
    }
