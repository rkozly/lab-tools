"""Per-device table view of a configuration file.

A :class:`TableDef` lists the columns shown for one remote file. Each
:class:`Column` has one or more candidate locations (``ParameterDef``); the
first candidate that exists in a device's file is shown and edited for that
device. AP-only columns are left empty for other devices.

Writes go through :meth:`ConfigService.write`, so only the edited values are
changed, a backup is saved, and the result is verified on the device.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Mapping

from devreader.config.parameters import ConfigFileDef, ParameterDef, params_from_dicts
from devreader.config.service import ConfigService, WriteResult

AP_NAME = "AP"

MAC_PATTERN = r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}"


@dataclass(frozen=True)
class Column:
    id: str
    label: str
    candidates: tuple[ParameterDef, ...]
    ap_only: bool = False
    tooltip: str = ""

    @property
    def read_only(self) -> bool:
        return all(p.read_only for p in self.candidates)


@dataclass(frozen=True)
class TableDef:
    id: str
    remote_path: str
    columns: tuple[Column, ...]
    format: str = "uci"
    read_only: bool = False
    # Show the "Flash wlan_wkk…" button (see devreader.core.firmware).
    flash_wlan: bool = False

    @property
    def title(self) -> str:
        return self.remote_path

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


@dataclass
class TableRead:
    serial: str
    cells: dict[str, Cell] = field(default_factory=dict)


def read_table(svc: ConfigService, serial: str, table: TableDef, *, is_ap: bool,
               cancel: threading.Event | None = None) -> TableRead:
    doc, _ = svc.read_document(serial, table.file_def(), cancel)
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


def write_table(svc: ConfigService, serial: str, table: TableDef, edits: Mapping[ParameterDef, str], *,
                cancel: threading.Event | None = None) -> WriteResult:
    """``edits`` maps the resolved ParameterDef of each changed cell to its validated value."""
    cfg = table.file_def(tuple(edits))
    return svc.write(serial, cfg, {p.id: v for p, v in edits.items()}, cancel=cancel)
