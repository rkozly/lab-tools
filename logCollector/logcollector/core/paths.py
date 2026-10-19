"""
Output path validation.

Called from worker threads (never the GUI thread) because touching a
network share that is down can block for many seconds.
"""

import os


class PathError(Exception):
    """Raised with a user-friendly message when a path cannot be used."""


def _unc_share_root(path: str) -> str | None:
    """Return \\\\server\\share for a UNC path, else None."""
    if not path.startswith("\\\\"):
        return None
    parts = path.lstrip("\\").split("\\")
    if len(parts) < 2:
        return None
    return "\\\\" + parts[0] + "\\" + parts[1]


def check_output_file(path: str) -> None:
    """Make sure `path` can be written. Creates the parent folder if needed.

    Raises PathError with a friendly message on failure.
    """
    path = path.strip()
    if not path:
        raise PathError("Output path is empty.")
    if os.path.isdir(path):
        raise PathError(f"Output path is a folder, not a file:\n{path}")

    share = _unc_share_root(path)
    if share and not os.path.exists(share):
        raise PathError(f"Network drive is not available:\n{share}")

    folder = os.path.dirname(path) or "."
    try:
        os.makedirs(folder, exist_ok=True)
    except PermissionError:
        raise PathError(f"Permission denied creating folder:\n{folder}")
    except OSError as e:
        raise PathError(f"Invalid output folder:\n{folder}\n({e.strerror or e})")

    # Write test without destroying an existing file.
    try:
        with open(path, "ab"):
            pass
    except PermissionError:
        raise PathError(f"Permission denied writing to:\n{path}")
    except OSError as e:
        raise PathError(f"Cannot write to:\n{path}\n({e.strerror or e})")


def check_input_file(path: str) -> None:
    path = path.strip()
    if not path:
        raise PathError("Input file is empty.")
    share = _unc_share_root(path)
    if share and not os.path.exists(share):
        raise PathError(f"Network drive is not available:\n{share}")
    if not os.path.isfile(path):
        raise PathError(f"Input file does not exist:\n{path}")
