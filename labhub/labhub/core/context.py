"""
AppContext: everything a plugin or the GUI needs, in one object.
Also holds the user settings file (%APPDATA%\\LabHub\\user.yaml).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from . import config as cfg
from .bbnet import BbNetService
from .topology import TopologyService
from .events import EventBus
from .live import BeaconStore, PollerManager, SampleStore
from .models import Lab
from .presets import PresetLibrary, parse_preset, preset_to_dict
from .registry import Registry
from .runner import JobRunner
from .shells import ShellManager
from .ramdump import RamdumpService
from .reboot import RebootService
from .targets import resolve

APP_DIR = Path(__file__).resolve().parent.parent.parent
USER_DIR = Path(os.environ.get("APPDATA", Path.home())) / "LabHub"
USER_FILE = USER_DIR / "user.yaml"
DEFAULT_EXPORT_DIR = Path.home() / "LabHub" / "exports"


class UserSettings:
    def __init__(self, path: Path = USER_FILE) -> None:
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                self.data = {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        os.replace(tmp, self.path)


class AppContext:
    def __init__(self, user: UserSettings | None = None) -> None:
        self.bus = EventBus()
        self.user = user or UserSettings()
        self.registry = Registry(self.bus)
        self.runner = JobRunner(self.bus, self.registry)
        self.pollers = PollerManager(self.bus, self.registry)
        self.shells = ShellManager(self.bus, self.registry)
        self.ramdumps = RamdumpService(self.bus, self.registry)
        self.reboots = RebootService(self.bus, self.registry, self.shells)
        self.bbnet = BbNetService(self.bus)
        self.topology = TopologyService(self.bus)
        self.samples = SampleStore(self.bus)
        self.beacons = BeaconStore(self.bus)
        self.presets = PresetLibrary()
        self.presets.load_builtin()
        self.presets.load_layer("user", self.user.get("presets", []))
        self.selection: list[str] = []
        self.lab_error: str | None = None

    # --- lab ----------------------------------------------------------------

    @property
    def lab(self) -> Lab:
        return self.registry.lab

    def lab_dirs(self) -> list[Path]:
        dirs = [Path(d) for d in self.user.get("lab_dirs", [])]
        dirs.append(APP_DIR / "labs")
        dirs.append(USER_DIR / "labs")
        seen, out = set(), []
        for d in dirs:
            k = str(d).lower()
            if k not in seen:
                seen.add(k)
                out.append(d)
        return out

    def available_labs(self) -> list[Path]:
        files = []
        for d in self.lab_dirs():
            if d.is_dir():
                files += sorted(p for p in d.glob("*.yaml") if not p.name.startswith("_"))
        return files

    def load_lab(self, path: str | Path | None) -> None:
        """Load a lab file, or build one from env variables when path is None."""
        self.lab_error = None
        if path is None:
            lab = cfg.lab_from_env()
        else:
            try:
                lab = cfg.load_lab(path)
            except cfg.ConfigError as e:
                self.lab_error = str(e)
                self.bus.log(f"Lab file error: {e}", "error")
                lab = cfg.lab_from_env()
            else:
                self.user.set("last_lab", str(path))
        self.presets.load_layer("lab", lab.presets)
        self.registry.set_lab(lab)

    def save_lab(self, lab: Lab, path: str | Path | None = None) -> Path:
        if path is None and not lab.path:
            USER_DIR.joinpath("labs").mkdir(parents=True, exist_ok=True)
            path = USER_DIR / "labs" / f"{lab.name.replace(' ', '_')}.yaml"
        p = cfg.save_lab(lab, path)
        self.user.set("last_lab", str(p))
        self.load_lab(p)
        return p

    # --- targets / presets --------------------------------------------------

    def resolve(self, targets: list[str]):
        return resolve(targets, self.registry.nodes(), self.lab.groups, self.selection)

    def save_user_presets(self) -> None:
        self.user.set("presets", [preset_to_dict(p) for p in self.presets.user_presets()])

    def put_preset(self, raw: dict) -> None:
        self.presets.put_user(parse_preset(raw, "user"))
        self.save_user_presets()

    @property
    def export_dir(self) -> Path:
        return Path(self.user.get("export_dir") or DEFAULT_EXPORT_DIR)

    def shutdown(self) -> None:
        self.ramdumps.stop_watch()
        self.shells.close_all()
        self.pollers.stop_all()
        self.runner.shutdown()
