"""Parser for ``network={ ... }`` blocks in wpa_supplicant configs.

Works on plain ``wpa_supplicant.conf`` files as well as shell scripts that
generate one (e.g. ``/lib/wifi/wpa_supplicant.sh``), where the blocks appear in
heredocs or ``echo`` lines. Shell constructs such as ``${var}`` are ignored when
matching braces, and commented-out lines are skipped.

Blocks are addressed as ``network[0]``, ``network[1]``, ... in file order.
A key that is absent from a block reads as ``None`` (shown as N/A).

:meth:`WpaNetworkDocument.set` replaces only the characters of an existing
value; every other byte of the file (shell code, line endings) is kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_BLOCK_START = re.compile(r"(?<![\w$])network\s*=\s*\{")
_SHELL_VAR = re.compile(r"\$\{[^}]*\}")
_KEY_VALUE = re.compile(r"(?<![\w$])(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$")
# Shell noise that can follow a value on echo / printf lines.
_VALUE_END = re.compile(r"""["']|\\n|\s+#|\s*>>?|\s*\|""")
_SELECTOR = re.compile(r"network\[(-?\d+)\]")
# Values set() may write: no quotes, spaces or shell characters.
_SAFE_VALUE = re.compile(r"[A-Za-z0-9.,:_-]+")


@dataclass
class WpaOption:
    key: str
    value: str
    line: int
    start: int = -1  # column of the value in the line
    end: int = -1


@dataclass
class WpaNetwork:
    index: int
    line: int
    options: dict[str, WpaOption] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"network block {self.index + 1} (line {self.line + 1})"


class WpaNetworkDocument:
    # Missing blocks are reported per parameter (N/A), not as a parse failure.
    require_sections = False

    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)
        self.changed_lines: set[int] = set()
        self._reparse()

    @classmethod
    def from_bytes(cls, data: bytes) -> "WpaNetworkDocument":
        return cls(data.decode("utf-8", "surrogateescape"))

    def to_bytes(self) -> bytes:
        return "".join(self._lines).encode("utf-8", "surrogateescape")

    def set(self, selector: str, key: str, value: str, section_type: str | None = None,
            create: bool = False) -> bool:
        """Replace an existing ``key=value`` in a block. Returns True if the file changed."""
        if create:
            raise ValueError("Adding keys to network blocks is not supported")
        if not _SAFE_VALUE.fullmatch(value):
            raise ValueError("Only letters, digits, '.', ',', ':', '_' and '-' are allowed")
        block = self.find_section(selector)
        if block is None:
            raise KeyError(f"Section '{selector}' not found")
        opt = block.options.get(key)
        if opt is None or opt.start < 0:
            raise KeyError(f"{key} not found in {block.label}")
        if opt.value == value:
            return False
        line = self._lines[opt.line]
        self._lines[opt.line] = line[:opt.start] + value + line[opt.end:]
        self.changed_lines.add(opt.line)
        self._reparse()
        return True

    def _reparse(self) -> None:
        self.text = "".join(self._lines)
        self.sections = []
        self._parse(self.text.splitlines())

    def _parse(self, lines: list[str]) -> None:
        current: WpaNetwork | None = None
        depth = 0
        for i, raw in enumerate(lines):
            if raw.strip().startswith("#"):
                continue
            match = _BLOCK_START.search(raw)
            if match:
                current = WpaNetwork(index=len(self.sections), line=i)
                self.sections.append(current)
                rest = _SHELL_VAR.sub("", raw[match.end():])
                depth = 1 + rest.count("{") - rest.count("}")
                if depth <= 0:
                    current = None
                continue
            if current is None:
                continue
            kv = _KEY_VALUE.search(raw)
            if kv and kv.group("key") not in current.options:  # first occurrence wins
                value, start, end = _clean_span(raw, kv.start("value"))
                current.options[kv.group("key")] = WpaOption(kv.group("key"), value, i, start, end)
            stripped = _SHELL_VAR.sub("", raw)
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                current = None

    def find_section(self, selector: str, section_type: str | None = None) -> WpaNetwork | None:
        match = _SELECTOR.fullmatch(selector.strip())
        if not match:
            return None
        try:
            return self.sections[int(match.group(1))]
        except IndexError:
            return None

    def get(self, selector: str, key: str, section_type: str | None = None) -> str | None:
        block = self.find_section(selector)
        if block is None:
            return None
        opt = block.options.get(key)
        return opt.value if opt else None


def _clean_span(line: str, pos: int) -> tuple[str, int, int]:
    """Value starting at ``line[pos]`` and its column span.

    ``58320" >> $CONF`` -> ``58320``; ``"My SSID"`` -> ``My SSID``.
    """
    while pos < len(line) and line[pos].isspace():
        pos += 1
    if line[pos:pos + 1] in ("'", '"'):
        end = line.find(line[pos], pos + 1)
        end = end if end > 0 else len(line)
        return line[pos + 1:end], pos + 1, end
    cut = _VALUE_END.search(line, pos)
    end = cut.start() if cut else len(line)
    while end > pos and line[end - 1].isspace():
        end -= 1
    return line[pos:end], pos, end
