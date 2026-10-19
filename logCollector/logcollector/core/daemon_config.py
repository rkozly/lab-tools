"""
Checking and changing the wpa_supplicant / hostapd start-up files.

Pure text helpers (no adb, no Qt) so they can be unit-tested; the tasks in
tasks/daemon_log_task.py pull the files, use these, and push them back.
"""

import re

from ..config import FileEdit

DONE = "done"        # change already in the file
NEEDED = "needed"    # original line found, change not applied yet
MISSING = "missing"  # neither the original nor the changed line found


def _pattern(text: str) -> re.Pattern:
    """Regex for `text` where any whitespace matches spaces/tabs."""
    return re.compile(r"[ \t]+".join(re.escape(t) for t in text.split()))


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def edit_status(text: str, edit: FileEdit) -> str:
    """DONE, NEEDED or MISSING for one edit in a file's text."""
    old = _pattern(edit.old)
    new = _pattern(edit.new) if edit.kind == "replace" else None
    found_done = found_needed = False
    for line in text.splitlines():
        if edit.kind == "replace":
            if _is_comment(line):
                continue
            if new.search(line):
                found_done = True
            elif old.search(line):
                found_needed = True
        elif old.search(line):  # "comment"
            if _is_comment(line):
                found_done = True
            else:
                found_needed = True
    if found_needed:
        return NEEDED
    return DONE if found_done else MISSING


def apply_edits(text: str, edits: list[FileEdit]) -> str:
    """Return `text` with every edit applied. Already-applied lines stay as they are."""
    # keepends: line endings of the device file are preserved exactly.
    lines = text.splitlines(keepends=True)
    for edit in edits:
        old = _pattern(edit.old)
        new = _pattern(edit.new) if edit.kind == "replace" else None
        for i, line in enumerate(lines):
            if _is_comment(line):
                continue
            if edit.kind == "replace":
                if not new.search(line) and old.search(line):
                    lines[i] = old.sub(lambda _m: edit.new, line, count=1)
            elif old.search(line):
                body = line.lstrip()
                lines[i] = line[:len(line) - len(body)] + "##" + body
    return "".join(lines)


# ----- running process --------------------------------------------------------

def process_list_command(binary: str) -> str:
    """Device shell command printing the full command line of each process."""
    return (f"for p in $(pidof {binary}); do "
            f"tr '\\000' ' ' < /proc/$p/cmdline; echo; done")


def pick_global_process(output: str) -> str | None:
    """The global daemon (started with -g) from process_list_command output."""
    lines = [" ".join(l.split()) for l in output.splitlines() if l.strip()]
    for line in lines:
        if " -g " in f" {line} ":
            return line
    return lines[0] if lines else None


def has_flags(cmdline: str, flags: list[str]) -> bool:
    """True if every flag (e.g. "-tddd" or "-f /tmp/x.log") is in cmdline."""
    padded = f" {' '.join(cmdline.split())} "
    return all(f" {' '.join(flag.split())} " in padded for flag in flags)
