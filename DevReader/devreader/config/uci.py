"""Formatting-preserving parser/editor for OpenWrt UCI files (``/etc/config/*``).

Most UCI libraries parse the file into a model and serialize it back, which
loses comments, blank lines, indentation and quoting. DevReader must change
*only* the targeted values, so this module works at the line level:

* the file is kept as a list of physical lines with their original endings;
* sections and options are indexed with the exact character span of each value;
* editing replaces just that span (or inserts one new line if the option is
  missing), so every other byte of the file is untouched.

Bytes are decoded with ``surrogateescape`` so non-UTF-8 content round-trips
unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ENCODING = "utf-8"
ERRORS = "surrogateescape"

_TOKEN = r"""(?:'(?P<{n}_sq>[^']*)'|"(?P<{n}_dq>(?:[^"\\]|\\.)*)"|(?P<{n}_bare>[^\s'"#]+))"""

_SECTION_RE = re.compile(
    r"^(?P<indent>\s*)config\s+" + _TOKEN.format(n="type")
    + r"(?:\s+" + _TOKEN.format(n="name") + r")?\s*(?:#.*)?$"
)
_OPTION_RE = re.compile(
    r"^(?P<indent>\s*)(?P<kind>option|list)\s+" + _TOKEN.format(n="key")
    + r"(?:\s+" + _TOKEN.format(n="val") + r")?(?P<rest>.*)$"
)
_SAFE_BARE = re.compile(r"^[A-Za-z0-9_.:/+@%,=-]+$")


def _token(match: re.Match, name: str) -> tuple[str | None, str, int, int]:
    """Return (value, quote_char, start, end) of the span *inside* the quotes."""
    for suffix, quote in (("sq", "'"), ("dq", '"'), ("bare", "")):
        group = f"{name}_{suffix}"
        if match.group(group) is not None:
            return match.group(group), quote, match.start(group), match.end(group)
    return None, "", -1, -1


@dataclass
class UciOption:
    key: str
    value: str
    line: int
    value_start: int  # -1 if the option has no value
    value_end: int
    quote: str
    kind: str = "option"


@dataclass
class UciSection:
    type: str
    name: str | None
    line: int
    indent: str = ""
    options: dict[str, UciOption] = field(default_factory=dict)
    lists: dict[str, list[UciOption]] = field(default_factory=dict)
    last_line: int = -1  # last option/list line belonging to the section

    @property
    def label(self) -> str:
        return f"{self.type} '{self.name}'" if self.name else f"{self.type} (anonymous)"


class UciDocument:
    def __init__(self, text: str) -> None:
        self._lines: list[tuple[str, str]] = []  # (content, line ending)
        for raw in text.splitlines(keepends=True):
            content = raw.rstrip("\r\n")
            self._lines.append((content, raw[len(content):]))
        self.sections: list[UciSection] = []
        self.changed_lines: set[int] = set()
        self._index()

    # -- construction / serialization ---------------------------------------
    @classmethod
    def from_bytes(cls, data: bytes) -> "UciDocument":
        return cls(data.decode(ENCODING, ERRORS))

    def to_text(self) -> str:
        return "".join(c + e for c, e in self._lines)

    def to_bytes(self) -> bytes:
        return self.to_text().encode(ENCODING, ERRORS)

    @property
    def line_count(self) -> int:
        return len(self._lines)

    def line(self, index: int) -> str:
        return self._lines[index][0]

    # -- indexing ------------------------------------------------------------
    def _index(self) -> None:
        self.sections = []
        current: UciSection | None = None
        for i, (content, _) in enumerate(self._lines):
            stripped = content.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _SECTION_RE.match(content)
            if m:
                stype, _, _, _ = _token(m, "type")
                sname, _, _, _ = _token(m, "name")
                current = UciSection(type=stype or "", name=sname, line=i, last_line=i)
                self.sections.append(current)
                continue
            m = _OPTION_RE.match(content)
            if m and current is not None:
                key, _, _, _ = _token(m, "key")
                value, quote, start, end = _token(m, "val")
                opt = UciOption(
                    key=key or "", value=value or "", line=i, value_start=start,
                    value_end=end, quote=quote, kind=m.group("kind"),
                )
                if opt.kind == "option":
                    current.options[opt.key] = opt  # last definition wins (UCI semantics)
                else:
                    current.lists.setdefault(opt.key, []).append(opt)
                if not current.indent:
                    current.indent = m.group("indent")
                current.last_line = i

    # -- lookup ----------------------------------------------------------------
    def find_section(self, selector: str, section_type: str | None = None) -> UciSection | None:
        """Locate a section.

        ``selector`` may be:
          * a section name (``'qcawifi'`` in ``config qcawifi 'qcawifi'``),
          * an extended UCI selector ``@type[index]`` (negative indexes allowed),
          * a section type (first section of that type) as a fallback.
        ``section_type`` restricts matches to that type.
        """
        m = re.fullmatch(r"@([^\[\]]+)\[(-?\d+)\]", selector)
        if m:
            typed = [s for s in self.sections if s.type == m.group(1)]
            try:
                return typed[int(m.group(2))]
            except IndexError:
                return None
        candidates = [s for s in self.sections if section_type is None or s.type == section_type]
        for s in candidates:
            if s.name == selector:
                return s
        for s in candidates:
            if s.type == selector:
                return s
        return None

    def get(self, selector: str, key: str, section_type: str | None = None) -> str | None:
        section = self.find_section(selector, section_type)
        if section is None:
            return None
        opt = section.options.get(key)
        return opt.value if opt else None

    # -- editing ---------------------------------------------------------------
    def set(self, selector: str, key: str, value: str, section_type: str | None = None,
            create: bool = True) -> bool:
        """Set ``option key value`` in the section. Returns True if the file changed."""
        if "\n" in value or "\r" in value:
            raise ValueError("Values must not contain line breaks")
        section = self.find_section(selector, section_type)
        if section is None:
            raise KeyError(f"Section '{selector}' not found")
        opt = section.options.get(key)
        if opt is not None:
            if opt.value == value and opt.value_start >= 0:
                return False
            self._replace_value(opt, value)
            return True
        if not create:
            raise KeyError(f"Option '{key}' not found in section {section.label}")
        self._insert_option(section, key, value)
        return True

    def _replace_value(self, opt: UciOption, value: str) -> None:
        content, ending = self._lines[opt.line]
        if opt.value_start < 0:  # "option key" without a value
            new = content.rstrip() + " " + _quote(value, "'")
        else:
            quote = opt.quote or ("" if _SAFE_BARE.match(value) else "'")
            if quote == "'" and "'" in value:
                raise ValueError("Values must not contain single quotes")
            if quote == '"':
                value = value.replace("\\", "\\\\").replace('"', '\\"')
            start, end = opt.value_start, opt.value_end
            if quote and not opt.quote:  # bare value that now needs quoting
                value = f"{quote}{value}{quote}"
            new = content[:start] + value + content[end:]
        self._lines[opt.line] = (new, ending)
        self.changed_lines.add(opt.line)
        self._index()

    def _insert_option(self, section: UciSection, key: str, value: str) -> None:
        at = section.last_line
        indent = section.indent or "\t"
        ending = self._lines[at][1] or self._default_ending()
        if not self._lines[at][1]:  # last line of file had no newline
            self._lines[at] = (self._lines[at][0], ending)
            ending = ""
        new_line = f"{indent}option {key} {_quote(value, chr(39))}"
        self._lines.insert(at + 1, (new_line, ending))
        self.changed_lines = {i + 1 if i > at else i for i in self.changed_lines}
        self.changed_lines.add(at + 1)
        self._index()

    def _default_ending(self) -> str:
        for _, e in self._lines:
            if e:
                return e
        return "\n"


def _quote(value: str, quote: str) -> str:
    if "'" in value:
        raise ValueError("Values must not contain single quotes")
    return f"{quote}{value}{quote}"
