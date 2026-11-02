"""
Lab configuration: load, validate, save, and create from environment variables.

Lab files are YAML (comments allowed) and are edited by hand or through the
mapping editor. Values may reference environment variables: ${env:NAME}.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

from .models import (CONNECT_TYPES, ROLES, BackboneConfig, BackboneLink,
                     ConnectSpec, DeviceConfig, Lab)

FORMAT_VERSION = 1
ENV_PREFIX = "adb_device_id_"
_ENV_REF = re.compile(r"\$\{env:([A-Za-z0-9_]+)\}")
_ENV_ROLE = re.compile(rf"^{ENV_PREFIX}(ap|cpe)(\d*)$", re.IGNORECASE)


class ConfigError(Exception):
    """A lab file is invalid. The message names the field."""


# --- environment ------------------------------------------------------------

def _registry_env() -> dict[str, str]:
    """User + system variables from the registry, so variables set with setx
    after the app started are seen too (os.environ is a startup snapshot)."""
    if sys.platform != "win32":
        return {}
    import winreg
    env: dict[str, str] = {}
    keys = [(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment")]  # user overrides system
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    if isinstance(value, str):
                        env[name] = value
                    i += 1
        except OSError:
            continue
    return env


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(_registry_env())
    return env


def expand_env(value: str, env: dict[str, str]) -> str:
    lower = {k.lower(): v for k, v in env.items()}
    return _ENV_REF.sub(lambda m: lower.get(m.group(1).lower(), "").strip(), value)


def lab_from_env(env: dict[str, str] | None = None, name: str = "From environment") -> Lab:
    """Build a lab from adb_device_id_ap / adb_device_id_cpe1..N.

    Serials are stored as ${env:...} references, so the file keeps following
    the variables. Backbones are added in the mapping editor (ssh target).
    """
    env = load_env() if env is None else env
    lab = Lab(name=name)
    found = []
    for var in env:
        m = _ENV_ROLE.match(var)
        if m and env[var].strip():
            role, num = m.group(1).lower(), m.group(2)
            found.append((0 if role == "ap" else 1, int(num or 0), role, num, var))
    for _, _, role, num, var in sorted(found):
        dev_id = f"{role}{num}"
        name_ = f"{role.upper()}{num}"
        lab.devices[dev_id] = DeviceConfig(
            id=dev_id, name=name_, role=role, serial=f"${{env:{var.lower()}}}")
    return lab


# --- parse / validate -------------------------------------------------------

def _need(d: dict, key: str, where: str, typ=str):
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"{where}.{key}: missing")
    if not isinstance(d[key], typ):
        raise ConfigError(f"{where}.{key}: must be {typ.__name__}")
    return d[key]


def _str_list(value, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ConfigError(f"{where}: must be a list")
    return [str(v) for v in value]


def _connect(raw, where: str) -> ConnectSpec:
    if raw is None:
        return ConnectSpec()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a mapping like {{type: ssh, host: ...}}")
    params = dict(raw)
    typ = str(params.pop("type", "none"))
    if typ not in CONNECT_TYPES:
        raise ConfigError(f"{where}.type: '{typ}' is not one of {', '.join(CONNECT_TYPES)}")
    return ConnectSpec(typ, params)


def parse_lab(data: dict, path: str | None = None) -> Lab:
    """Turn the YAML dict into a Lab. Raises ConfigError naming the bad field."""
    if not isinstance(data, dict):
        raise ConfigError("file must contain a mapping (key: value)")
    version = data.get("version", FORMAT_VERSION)
    if version != FORMAT_VERSION:
        raise ConfigError(f"version: {version} is not supported (expected {FORMAT_VERSION})")
    lab = Lab(name=str(data.get("lab") or Path(path or "lab").stem), path=path)

    for bid, raw in (data.get("backbones") or {}).items():
        where = f"backbones.{bid}"
        raw = raw or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: must be a mapping")
        extra = {k: v for k, v in raw.items() if k not in ("name", "connect")}
        lab.backbones[str(bid)] = BackboneConfig(
            str(bid), str(raw.get("name") or bid), _connect(raw.get("connect"), f"{where}.connect"), extra)

    for did, raw in (data.get("devices") or {}).items():
        where = f"devices.{did}"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: must be a mapping")
        role = str(raw.get("role", "cpe")).lower()
        if role not in ROLES:
            raise ConfigError(f"{where}.role: '{role}' is not one of {', '.join(ROLES)}")
        link = None
        bb = raw.get("backbone")
        if bb:
            if isinstance(bb, str):
                bb = {"id": bb}
            bid = _need(bb, "id", f"{where}.backbone")
            if bid not in lab.backbones:
                raise ConfigError(f"{where}.backbone.id: '{bid}' is not defined under backbones")
            link = BackboneLink(bid, _str_list(bb.get("interfaces"), f"{where}.backbone.interfaces"))
        known = ("name", "role", "serial", "backbone", "tags")
        lab.devices[str(did)] = DeviceConfig(
            id=str(did), name=str(raw.get("name") or did), role=role,
            serial=str(raw.get("serial") or ""), backbone=link,
            tags=_str_list(raw.get("tags"), f"{where}.tags"),
            extra={k: v for k, v in raw.items() if k not in known})

    for gname, members in (data.get("groups") or {}).items():
        lab.groups[str(gname)] = _str_list(members, f"groups.{gname}")

    lab.presets = data.get("presets") or []
    if not isinstance(lab.presets, list):
        raise ConfigError("presets: must be a list")
    lab.features = data.get("features") or {}
    if not isinstance(lab.features, dict):
        raise ConfigError("features: must be a mapping")
    dupes = set(lab.devices) & set(lab.backbones)
    if dupes:
        raise ConfigError(f"ids used for both a device and a backbone: {', '.join(sorted(dupes))}")
    return lab


def lab_to_dict(lab: Lab) -> dict:
    out: dict = {"version": FORMAT_VERSION, "lab": lab.name}
    out["backbones"] = {
        b.id: {"name": b.name, "connect": b.connect.to_dict(), **b.extra} for b in lab.backbones.values()}
    devices = {}
    for d in lab.devices.values():
        item: dict = {"name": d.name, "role": d.role, "serial": d.serial}
        if d.backbone:
            item["backbone"] = {"id": d.backbone.backbone_id}
            if d.backbone.interfaces:
                item["backbone"]["interfaces"] = list(d.backbone.interfaces)
        if d.tags:
            item["tags"] = list(d.tags)
        item.update(d.extra)
        devices[d.id] = item
    out["devices"] = devices
    if lab.groups:
        out["groups"] = {k: list(v) for k, v in lab.groups.items()}
    if lab.features:
        out["features"] = lab.features
    if lab.presets:
        out["presets"] = lab.presets
    return out


# --- files ------------------------------------------------------------------

def load_lab(path: str | Path) -> Lab:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name}: not valid YAML: {e}") from e
    except OSError as e:
        raise ConfigError(f"{path}: cannot read: {e.strerror}") from e
    try:
        return parse_lab(data, str(path))
    except ConfigError as e:
        raise ConfigError(f"{path.name}: {e}") from e


def save_lab(lab: Lab, path: str | Path | None = None) -> Path:
    path = Path(path or lab.path or "lab.yaml")
    header = "# LabHub lab file. Edit by hand or with the Mappings editor.\n" \
             "# Serial values may use ${env:adb_device_id_cpe1}.\n"
    text = yaml.safe_dump(lab_to_dict(lab), sort_keys=False, allow_unicode=True, width=120)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + text, encoding="utf-8")
    os.replace(tmp, path)
    lab.path = str(path)
    return path


def resolved_serial(dev: DeviceConfig, env: dict[str, str]) -> str:
    return expand_env(dev.serial, env).strip()
