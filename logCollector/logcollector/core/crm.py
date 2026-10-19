"""Locate the newest CRM build on the build share."""

import os

from ..config import CRM_PATTERN, CRM_ROOT, TRANSLATOR_SUBDIR


def pick_latest(names: list[str]) -> str | None:
    """Return the directory name with the highest build number, or None."""
    best, best_num = None, -1
    for name in names:
        m = CRM_PATTERN.match(name)
        if m and int(m.group(1)) > best_num:
            best, best_num = name, int(m.group(1))
    return best


def find_latest_crm(root: str = CRM_ROOT) -> str:
    """Return the full path of the latest CRM directory.

    Raises OSError if the share is not reachable, FileNotFoundError if no
    matching build exists.
    """
    names = os.listdir(root)  # may raise OSError (share unavailable)
    latest = pick_latest(names)
    if latest is None:
        raise FileNotFoundError(f"No matching CRM build found in {root}")
    return os.path.join(root, latest)


def translator_dir(crm_path: str) -> str:
    return os.path.join(crm_path, TRANSLATOR_SUBDIR)
