"""Per-device table view of a configuration file.

A :class:`TableDef` lists the columns shown for one remote file. Each
:class:`Column` has one or more candidate locations (``ParameterDef``); the
first candidate that exists in a device's file is shown and edited for that
device. AP-only columns are left empty for other devices.

A table with ``discover`` set has no fixed columns. It gets one column per
section that contains the option, read from each device's file. These are the
lines ``uci show <file> | grep <key>`` prints, e.g. ``wifi0`` or
``@wifi-iface[0]`` for ``disabled``. Devices whose files have different
sections each show the columns they have.

Writes go through :meth:`ConfigService.write`, so only the edited values are
changed, a backup is saved, and the result is verified on the device.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Mapping

from devreader.config.parameters import ConfigFileDef, ParameterDef, ParamType, params_from_dicts
from devreader.config.service import ConfigService, WriteResult

AP_NAME = "AP"

MAC_PATTERN = r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}"

# Options shown in the cell tooltip of a discovered column, to tell sections apart.
_INFO_KEYS = ("device", "mode", "ifname", "network")


@dataclass(frozen=True)
class Column:
    id: str
    label: str
    candidates: tuple[ParameterDef, ...]
    ap_only: bool = False
    tooltip: str = ""
    # Discovered columns only: sort position (section type, index among sections of that type).
    section_type: str = ""
    type_index: int = 0

    @property
    def read_only(self) -> bool:
        return all(p.read_only for p in self.candidates)


@dataclass(frozen=True)
class DiscoveredColumns:
    """One column per section that has ``option <key>``. All of them share the type and choices."""

    key: str
    type: ParamType = ParamType.STRING
    choices: tuple[str, ...] = ()
    choice_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableDef:
    id: str
    remote_path: str
    columns: tuple[Column, ...] = ()
    format: str = "uci"
    read_only: bool = False
    # Show the "Flash wlan_wkk…" button (see devreader.core.firmware).
    flash_wlan: bool = False
    # Tab text; defaults to remote_path. Needed when two tabs show the same file.
    tab_title: str = ""
    # Build the columns from each device's file instead of ``columns`` (UCI files only).
    discover: DiscoveredColumns | None = None

    @property
    def title(self) -> str:
        return self.tab_title or self.remote_path

    @property
    def config_name(self) -> str:
        """UCI config name as used by ``uci show``: the file name, e.g. ``wireless``."""
        return self.remote_path.rstrip("/").rsplit("/", 1)[-1]

    def file_def(self, parameters: tuple[ParameterDef, ...] = ()) -> ConfigFileDef:
        return ConfigFileDef(id=self.id, title=self.remote_path, remote_path=self.remote_path,
                             parameters=parameters, format=self.format, read_only=self.read_only)


def column(id: str, label: str, *candidates: dict, ap_only: bool = False, **common) -> Column:
    params = params_from_dicts(candidates, label=label, **common)
    tip = " or ".join(f"config {p.section_type or ''} '{p.section}' → option {p.key}".replace("  ", " ")
                      for p in params)
    return Column(id, label, params, ap_only, tip)


@dataclass
class Cell:
    param: ParameterDef | None  # None = not present on this device
    value: str | None = None
    info: str = ""  # tooltip text; empty = default


@dataclass
class TableRead:
    serial: str
    cells: dict[str, Cell] = field(default_factory=dict)
    # Discovered tables: this device's columns, in file order.
    columns: tuple[Column, ...] = ()


def read_table(svc: ConfigService, serial: str, table: TableDef, *, is_ap: bool,
               cancel: threading.Event | None = None) -> TableRead:
    doc, _ = svc.read_document(serial, table.file_def(), cancel)
    if table.discover is not None:
        return _read_discovered(serial, doc, table)
    result = TableRead(serial)
    for col in table.columns:
        cell = Cell(None)
        if is_ap or not col.ap_only:
            for p in col.candidates:
                section = doc.find_section(p.section, p.section_type)
                opt = section.options.get(p.key) if section is not None else None
                if opt is not None:
                    cell = Cell(p, opt.value)
                    break
        result.cells[col.id] = cell
    return result


def _read_discovered(serial: str, doc, table: TableDef) -> TableRead:
    spec = table.discover
    result = TableRead(serial)
    columns: list[Column] = []
    type_counts: dict[str, int] = {}
    for section in doc.sections:
        index = type_counts.get(section.type, 0)
        type_counts[section.type] = index + 1
        opt = section.options.get(spec.key)
        if opt is None:
            continue
        # Same names as `uci show`: named sections by name, anonymous ones as @type[index].
        # The index counts every section of that type, like libuci's @type[N] lookup
        # and UciDocument.find_section.
        ref = section.name or f"@{section.type}[{index}]"
        if ref in result.cells:  # duplicate section name: the first one is the one we edit
            continue
        path = f"{table.config_name}.{ref}.{spec.key}"
        param = ParameterDef(
            section=ref, key=spec.key, label=f"{ref}.{spec.key}", type=spec.type,
            section_type=section.type, choices=spec.choices, choice_labels=spec.choice_labels,
            description=path,
        )
        details = [f"{k} {section.options[k].value}" for k in _INFO_KEYS if k in section.options]
        info = path + (f"\n{', '.join(details)}" if details else "")
        columns.append(Column(ref, ref, (param,), tooltip=f"{path} (config {section.type})",
                              section_type=section.type, type_index=index))
        result.cells[ref] = Cell(param, opt.value, info)
    result.columns = tuple(columns)
    return result


def merge_columns(*column_lists: tuple[Column, ...]) -> list[Column]:
    """Union of discovered columns from several devices, ordered like ``uci show``.

    Columns are grouped by section type (types in order of first appearance)
    and sorted by their index within the type. Ties keep first-seen order.
    """
    merged: dict[str, Column] = {}
    for cols in column_lists:
        for c in cols:
            merged.setdefault(c.id, c)
    type_rank: dict[str, int] = {}
    for c in merged.values():
        type_rank.setdefault(c.section_type, len(type_rank))
    return sorted(merged.values(), key=lambda c: (type_rank[c.section_type], c.type_index))


def write_table(svc: ConfigService, serial: str, table: TableDef, edits: Mapping[ParameterDef, str], *,
                cancel: threading.Event | None = None) -> WriteResult:
    """``edits`` maps the resolved ParameterDef of each changed cell to its validated value."""
    cfg = table.file_def(tuple(edits))
    return svc.write(serial, cfg, {p.id: v for p, v in edits.items()}, cancel=cancel)
