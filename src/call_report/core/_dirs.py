"""Where call_report keeps files it downloads."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_APP_NAME = "call-report"
_DOWNLOAD_DIR_VARIABLE = "CALL_REPORT_DOWNLOAD_DIR"


def default_download_dir() -> Path:
    r"""Return the folder call_report keeps downloaded files in.

    The ``CALL_REPORT_DOWNLOAD_DIR`` environment variable is used if it is
    set. Otherwise the folder is ``call-report`` inside the platform's user
    data folder.

    - Windows: ``%LOCALAPPDATA%\call-report``
    - macOS: ``~/Library/Application Support/call-report``
    - Other platforms: ``$XDG_DATA_HOME/call-report``, or
      ``~/.local/share/call-report`` when ``XDG_DATA_HOME`` is not set.

    Each source keeps its files in its own subfolder. The folder is not
    created here.

    Returns
    -------
    pathlib.Path
        The download folder, with any ``~`` expanded.

    Examples
    --------
    >>> import os
    >>> from unittest import mock
    >>> with mock.patch.dict(os.environ, {"CALL_REPORT_DOWNLOAD_DIR": "/data/calls"}):
    ...     default_download_dir().as_posix()
    '/data/calls'
    """
    override = os.environ.get(_DOWNLOAD_DIR_VARIABLE)
    if override:
        return Path(override).expanduser()
    return _platform_data_dir() / _APP_NAME


def _platform_data_dir() -> Path:
    """Return the platform's folder for a user's application data.

    This is the folder the operating system expects an application to keep
    long lived user data in, as opposed to a cache.

    Returns
    -------
    pathlib.Path
        ``%LOCALAPPDATA%`` on Windows, ``~/Library/Application Support`` on
        macOS, and ``$XDG_DATA_HOME`` or ``~/.local/share`` elsewhere.
    """
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data)
        return Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    # The XDG Base Directory specification says a relative value is invalid
    # and is to be ignored.
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home and Path(xdg_data_home).is_absolute():
        return Path(xdg_data_home)
    return Path.home() / ".local" / "share"
