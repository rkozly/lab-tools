"""Declarative parameter definitions.

A :class:`ParameterDef` describes one editable value in a configuration file:
where it lives, how to label it, how to validate it and which widget to use.
The GUI builds its editor purely from these definitions, so adding a parameter
is a one-line change in :mod:`devreader.config.definitions`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from devreader.core.errors import ConfigValidationError


class ParamType(str, Enum):
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    CHOICE = "choice"
    BOOL = "bool"  # UCI convention: '1' / '0'


@dataclass(frozen=True)
class ParameterDef:
    section: str
    key: str
    label: str
    type: ParamType = ParamType.STRING
    section_type: str | None = None
    group: str = "General"
    unit: str = ""
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    pattern: str | None = None
    placeholder: str = ""
    read_only: bool = False
    required: bool = True  # if False, a missing option is not an error on read

    @property
    def id(self) -> str:
        return f"{self.section}.{self.key}"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ParameterDef":
        """Build from a plain dict, e.g. ``{"section": ..., "key": ..., "label": ...}``."""
        data = dict(data)
        if "type" in data:
            data["type"] = ParamType(data["type"])
        if "choices" in data:
            data["choices"] = tuple(str(c) for c in data["choices"])
        for alias, real in (("min", "minimum"), ("max", "maximum")):
            if alias in data:
                data[real] = data.pop(alias)
        return cls(**data)

    def validate(self, raw: str) -> str:
        """Validate/normalize a user-entered value. Returns the string to write."""
        value = (raw or "").strip()
        if value == "":
            raise ConfigValidationError(f"{self.label}: value is required")
        if any(c in value for c in "'\n\r"):
            raise ConfigValidationError(f"{self.label}: quotes and line breaks are not allowed")
        if self.type == ParamType.INT:
            if not re.fullmatch(r"[-+]?\d+", value):
                raise ConfigValidationError(f"{self.label}: '{value}' is not an integer")
            value = str(int(value))
            self._check_range(int(value))
        elif self.type == ParamType.FLOAT:
            try:
                number = float(value)
            except ValueError:
                raise ConfigValidationError(f"{self.label}: '{value}' is not a number") from None
            self._check_range(number)
        elif self.type == ParamType.CHOICE:
            if self.choices and value not in self.choices:
                raise ConfigValidationError(f"{self.label}: must be one of {', '.join(self.choices)}")
        elif self.type == ParamType.BOOL:
            mapping = {"1": "1", "0": "0", "true": "1", "false": "0", "on": "1", "off": "0",
                       "yes": "1", "no": "0", "enabled": "1", "disabled": "0"}
            if value.lower() not in mapping:
                raise ConfigValidationError(f"{self.label}: expected 1/0")
            value = mapping[value.lower()]
        if self.pattern and not re.fullmatch(self.pattern, value):
            raise ConfigValidationError(f"{self.label}: '{value}' has an invalid format")
        return value

    def _check_range(self, number: float) -> None:
        if self.minimum is not None and number < self.minimum:
            raise ConfigValidationError(f"{self.label}: must be >= {_fmt(self.minimum)}")
        if self.maximum is not None and number > self.maximum:
            raise ConfigValidationError(f"{self.label}: must be <= {_fmt(self.maximum)}")


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


@dataclass(frozen=True)
class ConfigFileDef:
    """A device configuration file and the parameters DevReader manages in it."""

    id: str
    title: str
    remote_path: str
    parameters: tuple[ParameterDef, ...]
    format: str = "uci"
    description: str = ""
    # Shell command(s) to run after a successful write, e.g. "wifi reload".
    # Empty by default: DevReader never restarts services implicitly.
    post_write_commands: tuple[str, ...] = ()
    groups: tuple[str, ...] = field(default=())
    # Read-only files get no Write button and are rejected by ConfigService.write.
    read_only: bool = False

    @property
    def group_order(self) -> list[str]:
        seen: list[str] = list(self.groups)
        for p in self.parameters:
            if p.group not in seen:
                seen.append(p.group)
        return seen

    def parameter(self, param_id: str) -> ParameterDef:
        for p in self.parameters:
            if p.id == param_id:
                return p
        raise KeyError(param_id)


def params_from_dicts(items: Sequence[Mapping[str, Any]], **defaults: Any) -> tuple[ParameterDef, ...]:
    return tuple(ParameterDef.from_dict({**defaults, **item}) for item in items)
