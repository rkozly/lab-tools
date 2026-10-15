"""Read-only parser for ``network={ ... }`` blocks in wpa_supplicant configs.

Works on plain ``wpa_supplicant.conf`` files as well as shell scripts that
generate one (e.g. ``/lib/wifi/wpa_supplicant.sh``), where the blocks appear in
heredocs or ``echo`` lines. Shell constructs such as ``${var}`` are ignored when
matching braces, and commented-out lines are skipped.

Blocks are addressed as ``network[0]``, ``network[1]``, ... in file order.
A key that is absent from a block reads as ``None`` (shown as N/A).
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


@dataclass
class WpaOption:
    key: str
    value: str
    line: int


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
        self.text = text
        self.sections: list[WpaNetwork] = []
        self._parse(text.splitlines())

    @classmethod
    def from_bytes(cls, data: bytes) -> "WpaNetworkDocument":
        return cls(data.decode("utf-8", "replace"))

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
                current.options[kv.group("key")] = WpaOption(kv.group("key"), _clean(kv.group("value")), i)
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


def _clean(value: str) -> str:
    """``58320" >> $CONF`` -> ``58320``; ``"My SSID"`` -> ``My SSID``."""
    value = value.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    cut = _VALUE_END.search(value)
    return (value[: cut.start()] if cut else value).strip()
