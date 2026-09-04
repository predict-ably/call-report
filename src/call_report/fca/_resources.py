"""Reading the JSON data files shipped inside the ``call_report.fca`` package.

Two kinds of generated metadata ship with the package: the canonical
schedule metadata under ``call_report/fca/data/schedules/`` and the
curated domain dataset definitions under
``call_report/fca/data/domain_datasets/``. Both are located and read the
same way, so that step lives here once. Each caller still parses what it
gets back into its own type.
"""

from __future__ import annotations

import importlib.resources

_DATA_PACKAGE = "call_report.fca"
_DATA_DIRECTORY = "data"


def read_packaged_json_text(*, subdirectory: str, name: str) -> str:
    """Read one packaged JSON file's text.

    Parameters
    ----------
    subdirectory : str
        The directory under ``call_report/fca/data/`` holding the file,
        e.g. ``"schedules"`` or ``"domain_datasets"``.
    name : str
        The file's name without its ``.json`` extension, e.g. ``"RCB"``.

    Returns
    -------
    str
        The file's decoded contents, still unparsed.

    Examples
    --------
    >>> from call_report.fca._resources import read_packaged_json_text
    >>> text = read_packaged_json_text(subdirectory="schedules", name="RCB")
    >>> text.startswith("{")
    True
    """
    resource = importlib.resources.files(_DATA_PACKAGE).joinpath(
        _DATA_DIRECTORY, subdirectory, f"{name}.json"
    )
    return resource.read_text(encoding="utf-8")
