"""
Mapping editor: one table, one row per device:

    Name | Device id | Serial | Backbone (ssh) | Namespace

Backbone is an ssh target like `root@10.0.0.21` or `10.0.0.21:2222`
(user defaults to root). Namespace is optional: backbone commands then run
inside `ip netns exec <ns>` (via sudo for non-root users). The BB password
(ssh + sudo) is set here and saved in the lab file; empty = the ssh_pass
environment variable, else the lab default.
"""

from __future__ import annotations

import copy
import os
import re

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..core import config as cfg
from ..core import license as lic
from ..core.models import (BackboneConfig, BackboneLink, ConnectSpec,
                           DeviceConfig, Lab)
from ..transports.ssh_exec import DEFAULT_PASSWORD, PASS_ENV

COLS = ("Name", "Device id", "Serial", "Backbone  (ssh user@host)", "Namespace", "BB interfaces  (control, data)",
        "Device host  (empty = this PC)")
_HOST = re.compile(r"^[\w.-]+$")
_SSH = re.compile(r"^(?:(?P<user>[^@\s]+)@)?(?P<host>[^:\s]+)(?::(?P<port>\d+))?$")
_IFACE = re.compile(r"^[\w.@:-]+$")


def _natural(d) -> tuple:
    """AP first, then CPE1, CPE2 ... CPE10 (by number, not as text: CPE10 is not before CPE2)."""
    digits = "".join(ch for ch in d.name if ch.isdigit())
    return (role_of(d.name) != "ap", int(digits) if digits else 0, d.name.lower())


def role_of(name: str) -> str:
    n = name.strip().upper()
    return "ap" if n.startswith("AP") else ("cpe" if n.startswith("CPE") else "other")


def ssh_text(bb: BackboneConfig | None) -> str:
    if bb is None or bb.connect.type != "ssh":
        return ""
    p = bb.connect.params
    user = p.get("user") or "root"
    text = f"{user}@{p.get('host', '')}"
    if p.get("port") and str(p["port"]) != "22":
        text += f":{p['port']}"
    return text


def parse_ssh(text: str) -> dict | None:
    m = _SSH.match(text.strip())
    if not m:
        return None
    params = {"host": m.group("host"), "user": m.group("user") or "root"}
    if m.group("port"):
        params["port"] = int(m.group("port"))
    return params


class MappingEditor(QDialog):
    def __init__(self, parent, lab: Lab, seen_serials: list[str], assign_serial: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Device mappings")
        self.resize(900, 520)
        self.lab = copy.deepcopy(lab)
        self.seen = seen_serials
        self.result_lab: Lab | None = None
        self.save_as: str | None = None
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Lab name"))
        self.name = QLineEdit(self.lab.name)
        top.addWidget(self.name, 1)
        where = QLabel(self.lab.path or "(will be saved to your user folder)")
        where.setObjectName("Muted")
        top.addWidget(where, 2)
        lay.addLayout(top)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().hide()
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Stretch)
        h.setSectionResizeMode(4, QHeaderView.Interactive)
        self.table.setColumnWidth(4, 110)
        h.setSectionResizeMode(5, QHeaderView.Interactive)
        self.table.setColumnWidth(5, 220)
        h.setSectionResizeMode(6, QHeaderView.Interactive)
        self.table.setColumnWidth(6, 190)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        for text, fn in (("+ Add device", lambda: self._add()), ("Remove", self._remove),
                         ("Import from env vars", self._import_env)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(QLabel("BB password (ssh + sudo)"))
        env_pw = {k.lower(): v for k, v in cfg.load_env().items()}.get(PASS_ENV)
        self.password = QLineEdit(str((self.lab.features or {}).get("ssh_password") or env_pw or DEFAULT_PASSWORD))
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setFixedWidth(200)
        self.password.setToolTip("Used to log in to the backbones and for sudo. Saved in the lab file.")
        row.addWidget(self.password)
        show = QPushButton("Show")
        show.setCheckable(True)
        show.setFixedWidth(52)
        show.setToolTip("Show / hide the password")
        show.toggled.connect(lambda on: self.password.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        row.addWidget(show)
        lay.addLayout(row)
        hint = QLabel("Serial: pick from what adb sees now, or type ${env:adb_device_id_cpe1}.   "
                      "Backbone: root@lx-lbr-45 (port: root@host:2222), empty = none.   "
                      "Namespace: e.g. ns0, empty = none.\n"
                      "BB interfaces: control (.960) then data (AP v10371015, CPE .1015), e.g. AP.960 v10371015 – "
                      "empty = ap.960 v10371015 / cpe1.960 cpe1.1015. Used by the IPv6 check, DHCP server / dhclient buttons.")
        hint.setObjectName("Muted")
        lay.addWidget(hint)

        lic_row = QHBoxLayout()
        lic_row.addWidget(QLabel("AP license file"))
        lic_file, lic_dest = lic.settings(self.lab.features)
        self.lic_file = QLineEdit(lic_file)
        self.lic_file.setToolTip("Pushed to the AP by AP tab → 🔑 Push license (adb push)")
        lic_row.addWidget(self.lic_file, 3)
        b = QPushButton("…")
        b.setToolTip("Pick the license file")
        b.clicked.connect(self._pick_license)
        lic_row.addWidget(b)
        lic_row.addWidget(QLabel("→ on the AP"))
        self.lic_dest = QLineEdit(lic_dest)
        lic_row.addWidget(self.lic_dest, 1)
        lay.addLayout(lic_row)

        self._extra: dict[str, dict] = {}
        for d in sorted(self.lab.devices.values(), key=_natural):
            bb = self.lab.backbones.get(d.backbone.backbone_id) if d.backbone else None
            ns = bb.connect.params.get("netns", "") if bb else ""
            ifaces = " ".join(d.backbone.interfaces) if d.backbone else ""
            self._add(d.name, d.id, d.serial, ssh_text(bb), str(ns), ifaces, str(d.extra.get("host") or ""))
            self._extra[d.id] = d.extra
        if assign_serial:
            self._add("", "", assign_serial, "", "")
            self.table.scrollToBottom()
        if not self.lab.devices and not assign_serial:
            self._import_env(quiet=True)

        self.error = QLabel()
        self.error.setStyleSheet("color: #ff6b7a;")
        lay.addWidget(self.error)
        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.addButton("Save as…", QDialogButtonBox.ActionRole).clicked.connect(self._save_as)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def _add(self, name: str = "", did: str = "", serial: str = "", ssh: str = "", ns: str = "",
             ifaces: str = "", host: str = "") -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        if not name:
            n = sum(1 for i in range(r) if role_of(self._text(i, 0)) == "cpe") + 1
            name, did = f"CPE{n}", did or f"cpe{n}"
        self.table.setItem(r, 0, QTableWidgetItem(name))
        self.table.setItem(r, 1, QTableWidgetItem(did or name.lower().replace(" ", "")))
        ser = QComboBox()
        ser.setEditable(True)
        ser.addItems([serial] + [s for s in self.seen if s != serial])
        ser.setCurrentText(serial)
        self.table.setCellWidget(r, 2, ser)
        self.table.setItem(r, 3, QTableWidgetItem(ssh))
        self.table.setItem(r, 4, QTableWidgetItem(ns))
        it = QTableWidgetItem(ifaces)
        it.setToolTip("Control and data interface on the backbone, e.g. CPE1.960 CPE1.1015.\n"
                      "Empty = <name>.960 + v10371015 (AP) or <name>.1015 (CPE), lower case, e.g. cpe1.1015")
        self.table.setItem(r, 5, it)
        hi = QTableWidgetItem(host)
        hi.setToolTip("The Windows PC this device is plugged into (USB), e.g. lab-pc-2.\n"
                      "Empty = this PC. LabHub reaches its adb through ssh (your Windows user, the BB password).")
        self.table.setItem(r, 6, hi)

    def _remove(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()} or
                      ({self.table.currentRow()} if self.table.currentRow() >= 0 else set()), reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _import_env(self, quiet: bool = False) -> None:
        env_lab = cfg.lab_from_env()
        have = {self._text(r, 1) for r in range(self.table.rowCount())}
        added = 0
        for d in env_lab.devices.values():
            if d.id not in have:
                self._add(d.name, d.id, d.serial, "", "")
                added += 1
        if not quiet:
            QMessageBox.information(self, "Import", f"Added {added} device(s) from adb_device_id_* variables.")

    def _pick_license(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "AP license file", self.lic_file.text(),
                                              "License (*.pfm);;All files (*)")
        if path:
            self.lic_file.setText(os.path.normpath(path))

    def _text(self, r: int, c: int) -> str:
        it = self.table.item(r, c)
        return it.text().strip() if it else ""

    def _collect(self) -> Lab:
        features = dict(self.lab.features)
        if self.password.text():
            features["ssh_password"] = self.password.text()
        else:
            features.pop("ssh_password", None)
        dest = self.lic_dest.text().strip()
        if dest and not dest.startswith("/"):
            raise cfg.ConfigError(f"license folder on the AP '{dest}' must start with /")
        features["license"] = {"file": self.lic_file.text().strip() or lic.DEFAULT_FILE,
                               "dest": dest or lic.DEFAULT_DEST}
        lab = Lab(name=self.name.text().strip() or "Lab", path=self.lab.path, groups=self.lab.groups,
                  features=features, presets=self.lab.presets)
        for r in range(self.table.rowCount()):
            name, did = self._text(r, 0), self._text(r, 1)
            if not name and not did:
                continue
            did = did or name.lower().replace(" ", "")
            name = name or did.upper()
            if not re.fullmatch(r"[\w.-]+", did):
                raise cfg.ConfigError(f"row {r + 1}: device id '{did}' may only use letters, digits, _ . -")
            if did in lab.devices:
                raise cfg.ConfigError(f"device id '{did}' is used twice")
            link = None
            ssh, ns = self._text(r, 3), self._text(r, 4)
            if ns and not ssh:
                raise cfg.ConfigError(f"{name}: namespace '{ns}' needs a backbone host")
            if ns and not re.fullmatch(r"[\w.-]+", ns):
                raise cfg.ConfigError(f"{name}: namespace '{ns}' may only use letters, digits, _ . -")
            if ssh:
                params = parse_ssh(ssh)
                if params is None:
                    raise cfg.ConfigError(f"{name}: backbone '{ssh}' is not like root@10.0.0.21")
                if ns:
                    params["netns"] = ns
                bid = f"{did}-bb"
                lab.backbones[bid] = BackboneConfig(bid, f"{name} BB", ConnectSpec("ssh", params))
                ifaces = self._text(r, 5).replace(",", " ").split()
                bad = [i for i in ifaces if not _IFACE.match(i)]
                if bad:
                    raise cfg.ConfigError(f"{name}: interface '{bad[0]}' is not a valid name")
                link = BackboneLink(bid, ifaces)
            extra = dict(self._extra.get(did, {}))
            host = self._text(r, 6)
            if host and not _HOST.match(host):
                raise cfg.ConfigError(f"{name}: device host '{host}' is not a host name (e.g. lab-pc-2)")
            if host:
                extra["host"] = host           # the PC the device is plugged into
            else:
                extra.pop("host", None)
            lab.devices[did] = DeviceConfig(did, name, role_of(name), self.table.cellWidget(r, 2).currentText().strip(),
                                            link, [], extra)
        cfg.parse_lab(cfg.lab_to_dict(lab))  # same validation as for files
        return lab

    def _old_interfaces(self, did: str) -> list[str]:
        d = self.lab.devices.get(did)
        return list(d.backbone.interfaces) if d and d.backbone else []

    def _save(self) -> None:
        try:
            self.result_lab = self._collect()
        except (cfg.ConfigError, ValueError) as e:
            self.error.setText(str(e))
            return
        self.accept()

    def _save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save lab file", f"{self.name.text() or 'lab'}.yaml",
                                              "Lab files (*.yaml)")
        if path:
            self.save_as = path
            self._save()
