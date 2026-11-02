"""
Preset library: loads presets from three layers (built-in < lab < user),
expands placeholders, and saves user edits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

KINDS = ("exec", "sequence", "monitor")
BUILTIN_FILE = Path(__file__).resolve().parent.parent / "presets" / "builtin.yaml"
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PresetError(Exception):
    pass


@dataclass
class Param:
    choices: dict[str, str] = field(default_factory=dict)  # label -> value
    default: str = ""


@dataclass
class SequenceSpec:
    start: str
    steps: list[str]
    prompt: str = r"[:>?]\s*$"
    delay_ms: int = 500
    step_timeout_s: float = 15.0
    tty: bool = False
    exit: str | None = None
    groups: list[list[str]] = field(default_factory=list)  # e.g. [["1","2","1"], ["4","29","1"]]
    success: str = ""          # regex that must appear after each group ("" = no check)
    settle_ms: int = 800       # output must be quiet this long before the next key


@dataclass
class MonitorSpec:
    command: str
    parser: str
    interval_s: float = 1.0


@dataclass
class Preset:
    id: str
    name: str
    category: str = "General"
    roles: list[str] = field(default_factory=list)  # empty = any
    kind: str = "exec"
    script: str = ""
    params: dict[str, Param] = field(default_factory=dict)
    sequence: SequenceSpec | None = None
    monitor: MonitorSpec | None = None
    source: str = "builtin"  # builtin | lab | user

    def fits(self, role: str) -> bool:
        return not self.roles or role in self.roles


def parse_preset(raw: dict, source: str) -> Preset:
    if not isinstance(raw, dict) or not raw.get("id"):
        raise PresetError(f"{source} preset without id: {raw!r}")
    pid = str(raw["id"])
    kind = str(raw.get("kind", "exec"))
    if kind not in KINDS:
        raise PresetError(f"preset {pid}: kind '{kind}' is not one of {', '.join(KINDS)}")
    params = {}
    for pname, p in (raw.get("params") or {}).items():
        p = p or {}
        params[str(pname)] = Param({str(k): str(v) for k, v in (p.get("choices") or {}).items()},
                                   str(p.get("default", "")))
    seq = mon = None
    if kind == "sequence":
        s = raw.get("sequence") or {}
        if not s.get("start") or not (s.get("steps") or s.get("groups")):
            raise PresetError(f"preset {pid}: sequence needs start and groups (or steps)")
        if s.get("groups"):
            if not all(isinstance(g, list) and g for g in s["groups"]):
                raise PresetError(f"preset {pid}: groups must be lists like [1, 2, 1]")
            groups = [[str(x) for x in g] for g in s["groups"]]
        else:
            groups = [[str(x)] for x in s["steps"]]
        seq = SequenceSpec(str(s["start"]), [k for g in groups for k in g],
                           str(s.get("prompt", SequenceSpec.prompt)), int(s.get("delay_ms", 500)),
                           float(s.get("step_timeout_s", 15)), bool(s.get("tty", False)), s.get("exit"),
                           groups, str(s.get("success", "") or ""), int(s.get("settle_ms", 800)))
        if seq.success:
            try:
                re.compile(seq.success)
            except re.error as e:
                raise PresetError(f"preset {pid}: success is not a valid regex: {e}") from e
        try:
            re.compile(seq.prompt)
        except re.error as e:
            raise PresetError(f"preset {pid}: prompt is not a valid regex: {e}") from e
    if kind == "monitor":
        m = raw.get("monitor") or {}
        if not m.get("command") or not m.get("parser"):
            raise PresetError(f"preset {pid}: monitor needs command and parser")
        mon = MonitorSpec(str(m["command"]), str(m["parser"]), float(m.get("interval_s", 1)))
    elif kind == "exec" and not str(raw.get("script", "")).strip():
        raise PresetError(f"preset {pid}: script is empty")
    roles = raw.get("roles") or []
    return Preset(pid, str(raw.get("name") or pid), str(raw.get("category") or "General"),
                  [str(r) for r in ([roles] if isinstance(roles, str) else roles)],
                  kind, str(raw.get("script", "")).rstrip("\n"), params, seq, mon, source)


def preset_to_dict(p: Preset) -> dict:
    out: dict = {"id": p.id, "name": p.name, "category": p.category}
    if p.roles:
        out["roles"] = list(p.roles)
    if p.kind != "exec":
        out["kind"] = p.kind
    if p.params:
        out["params"] = {k: ({"choices": dict(v.choices)} if v.choices else {}) |
                         ({"default": v.default} if v.default else {}) for k, v in p.params.items()}
    if p.script:
        out["script"] = p.script
    if p.sequence:
        s = p.sequence
        out["sequence"] = {"start": s.start, "groups": [list(g) for g in s.groups],
                           "success": s.success, "delay_ms": s.delay_ms, "settle_ms": s.settle_ms,
                           "step_timeout_s": s.step_timeout_s}
        if s.exit:
            out["sequence"]["exit"] = s.exit
    if p.monitor:
        out["monitor"] = {"command": p.monitor.command, "parser": p.monitor.parser,
                          "interval_s": p.monitor.interval_s}
    return out


def expand(text: str, values: dict[str, str]) -> str:
    """Replace {key} placeholders; unknown ones are left as they are."""
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


class PresetLibrary:
    """Ordered presets; later layers override earlier ones by id."""

    def __init__(self) -> None:
        self._layers: dict[str, list[Preset]] = {"builtin": [], "lab": [], "user": []}
        self.errors: list[str] = []

    def load_layer(self, source: str, raws: list) -> None:
        items = []
        for raw in raws or []:
            try:
                items.append(parse_preset(raw, source))
            except PresetError as e:
                self.errors.append(str(e))
        self._layers[source] = items

    def load_builtin(self, path: Path = BUILTIN_FILE) -> None:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.load_layer("builtin", data.get("presets") or [])

    def all(self) -> list[Preset]:
        order: list[str] = []
        merged: dict[str, Preset] = {}
        for layer in ("builtin", "lab", "user"):
            for p in self._layers[layer]:
                if p.id not in merged:
                    order.append(p.id)
                merged[p.id] = p
        return [merged[i] for i in order]

    def get(self, pid: str) -> Preset | None:
        return next((p for p in self.all() if p.id == pid), None)

    def categories(self) -> list[str]:
        seen: list[str] = []
        for p in self.all():
            if p.category not in seen:
                seen.append(p.category)
        return seen

    def user_presets(self) -> list[Preset]:
        return list(self._layers["user"])

    def put_user(self, preset: Preset) -> None:
        preset.source = "user"
        items = [p for p in self._layers["user"] if p.id != preset.id]
        items.append(preset)
        self._layers["user"] = items

    def delete_user(self, pid: str) -> bool:
        before = len(self._layers["user"])
        self._layers["user"] = [p for p in self._layers["user"] if p.id != pid]
        return len(self._layers["user"]) != before

    def is_overridden_builtin(self, pid: str) -> bool:
        return any(p.id == pid for p in self._layers["builtin"]) and \
            any(p.id == pid for p in self._layers["user"])
