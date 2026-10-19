"""Read-only parser for JSON files such as ``/firmware/verinfo/ver_info.txt``.

Each top-level object is a section addressed by its key (``Image_Build_IDs``,
``Metabuild_Info``, ...); its scalar members are the options. ``null`` and
missing keys read as ``None`` (shown as N/A).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class JsonOption:
    key: str
    value: str


@dataclass
class JsonSection:
    name: str
    options: dict[str, JsonOption] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"\"{self.name}\""


class JsonDocument:
    # Missing sections are reported per parameter (N/A), not as a parse failure.
    require_sections = False

    def __init__(self, text: str) -> None:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("top level is not a JSON object")
        self.sections: dict[str, JsonSection] = {}
        for name, members in data.items():
            if not isinstance(members, dict):
                continue
            section = JsonSection(name)
            for key, value in members.items():
                if value is not None and not isinstance(value, (dict, list)):
                    section.options[key] = JsonOption(key, str(value))
            self.sections[name] = section

    @classmethod
    def from_bytes(cls, data: bytes) -> "JsonDocument":
        return cls(data.decode("utf-8-sig", "replace"))

    def find_section(self, selector: str, section_type: str | None = None) -> JsonSection | None:
        return self.sections.get(selector.strip())

    def get(self, selector: str, key: str, section_type: str | None = None) -> str | None:
        section = self.find_section(selector)
        opt = section.options.get(key) if section else None
        return opt.value if opt else None


def set_string_value(data: bytes, section: str, key: str, value: str) -> bytes:
    """Return ``data`` with ``"key": "<old>"`` inside top-level ``section`` set to ``value``.

    Only the characters of the old string change; indentation, key order and
    line endings stay as they are. Raises KeyError if the key is not a string
    member of the section.
    """
    text = data.decode("utf-8-sig")
    doc = json.loads(text)
    if not isinstance(doc.get(section), dict) or not isinstance(doc[section].get(key), str):
        raise KeyError(f'"{key}" not found in "{section}"')
    start = re.search(r'"%s"\s*:\s*\{' % re.escape(section), text)
    if not start:
        raise KeyError(f'"{section}" not found')
    end = text.find("}", start.end())  # sections hold only scalars
    member = re.compile(r'("%s"\s*:\s*)"((?:[^"\\]|\\.)*)"' % re.escape(key))
    m = member.search(text, start.end(), end)
    if not m:
        raise KeyError(f'"{key}" not found in "{section}"')
    new_text = text[:m.start(2)] + json.dumps(value)[1:-1] + text[m.end(2):]
    if json.loads(new_text)[section][key] != value:
        raise ValueError("Internal check failed: edit did not produce the expected value")
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    return bom + new_text.encode("utf-8")
