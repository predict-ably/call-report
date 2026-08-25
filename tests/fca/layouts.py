"""Reusable FCA layout variable-line blocks for the fca test fixtures.

These are plain data, imported by name from ``tests/fca/conftest.py`` and
from individual test modules. They live outside ``conftest.py`` for the
reason given in :mod:`tests.helpers`: pytest loads every ``conftest.py``
itself, so importing one as a library gives it two identities in
``sys.modules``.

Every block mimics a structural scenario seen in real FCA releases, but
is entirely hand-written. Nothing here is read from ``references/``.
"""

from __future__ import annotations

RC_LINES_7COL = [
    "  SYSTEM     Numeric    0  System Code",
    "  DIST       Numeric    0  District Code",
    "  ASSOC      Numeric    0  Association Code",
    "  MONTH      Numeric    0  Month of Report",
    "  YEAR       Numeric    0  Year of Report",
    "  UNINUM     Numeric    0  Unique institution number",
    "  TOTASSETS  Numeric    0  Total Assets",
]

RC_LINES_8COL = [
    *RC_LINES_7COL,
    "  TOTLIAB    Numeric    0  Total Liabilities (added later)",
]

RCB_LINES = [
    "  SYSTEM      Numeric    0  System Code",
    "  DIST        Numeric    0  District Code",
    "  ASSOC       Numeric    0  Association Code",
    "  MONTH       Numeric    0  Month of Report",
    "  YEAR        Numeric    0  Year of Report",
    "  UNINUM      Numeric    0  Unique institution number",
    "  **INV_CODE  Numeric    0  Investment code: 10 Cash  20 Securities  30 Loans",
    "  **AMOUNT    Numeric    0  Amount",
    "  **AMOUNT2   Numeric    2  Amount 2",
]

RCR7_LINES = [
    "  SYSTEM     Numeric   0  System Code",
    "  DIST       Numeric   0  District Code",
    "  ASSOC      Numeric   0  Association Code",
    "  MONTH      Numeric   0  Month of Report",
    "  YEAR       Numeric   0  Year of Report",
    "  UNINUM     Numeric   0  Unique institution number",
    "  **CAPCODE  Numeric   0  Capital code: 10 Beginning  20 Ending",
    "  **VAL1     Numeric   0  Value 1",
    "  **VAL2     Numeric   0  Value 2",
    "  TOTAL      Numeric   0  Total amount",
]

INST_LINES = [
    "  SYSTEM     Numeric    0  System Code",
    "  DIST       Numeric    0  District Code",
    "  ASSOC      Numeric    0  Association Code",
    "  MONTH      Numeric    0  Month of Report",
    "  YEAR       Numeric    0  Year of Report",
    "  UNINUM     Numeric    0  Unique institution number",
    "  SHORTNAME  Alphanum.  0  Institution short name",
    "  STATE      Alphanum.  0  State code",
]
