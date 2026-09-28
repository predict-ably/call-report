"""Shared roster rows for the FCA institution and registry tests."""

from __future__ import annotations

from typing import Any

ROW: dict[str, Any] = {
    "UNINUM": 722825,
    "SYSTEM": 7,
    "DIST": 22,
    "ASSOC": 825,
    "SHORTNAME": "Mid-America ACA",
    "MAIL_ADDR": "P.O. Box 34390",
    "STREET_ADDR": "1601 UPS Drive",
    "CITY": "Louisville",
    "STATE": "KY",
    "ZIP": "40223-4390",
}

FRAME_COLUMNS = [
    "UNINUM",
    "period",
    "SYSTEM",
    "DIST",
    "ASSOC",
    "SHORTNAME",
    "MAIL_ADDR",
    "STREET_ADDR",
    "CITY",
    "STATE",
    "ZIP",
    "most_recent_short_name",
    "most_recent_mail_addr",
    "most_recent_street_addr",
    "most_recent_city",
    "most_recent_state",
    "most_recent_zip",
]
