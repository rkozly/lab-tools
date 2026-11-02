"""
Target resolver. A target string selects nodes:

    cpe1            one node by id (or by name, case-insensitive)
    role:cpe        all CPEs            role:ap         all APs
    kind:backbone   all backbones       kind:device     all APs and CPEs
    group:<name>    a group from the lab file
    tag:<tag>       devices with that tag
    selected        the current tile selection
    all             every configured node

Results come back in display order, without duplicates.
"""

from __future__ import annotations

from .models import DeviceConfig, NodeView

QUICK_TARGETS = [
    ("Selected", "selected"),
    ("All APs", "role:ap"),
    ("All CPEs", "role:cpe"),
    ("All Backbones", "kind:backbone"),
    ("All devices", "kind:device"),
    ("Everything", "all"),
]


class TargetError(Exception):
    pass


def resolve(targets: list[str], nodes: list[NodeView], groups: dict[str, list[str]],
            selected: list[str] | None = None, _depth: int = 0) -> list[NodeView]:
    if _depth > 5:
        raise TargetError("groups refer to each other in a loop")
    chosen: set[str] = set()
    by_id = {n.id: n for n in nodes}
    by_name = {n.name.lower(): n for n in nodes}
    for raw in targets:
        t = raw.strip()
        low = t.lower()
        if not t:
            continue
        if low == "all":
            chosen |= {n.id for n in nodes if n.kind != "unassigned"}
        elif low == "selected":
            chosen |= {i for i in (selected or []) if i in by_id}
        elif low.startswith("role:"):
            role = low[5:]
            chosen |= {n.id for n in nodes if isinstance(n.config, DeviceConfig) and n.config.role == role}
        elif low.startswith("kind:"):
            kind = low[5:]
            chosen |= {n.id for n in nodes if n.kind == kind}
        elif low.startswith("tag:"):
            tag = t[4:]
            chosen |= {n.id for n in nodes if isinstance(n.config, DeviceConfig) and tag in n.config.tags}
        elif low.startswith("group:"):
            name = t[6:]
            if name not in groups:
                raise TargetError(f"unknown group '{name}'")
            chosen |= {n.id for n in resolve(groups[name], nodes, groups, selected, _depth + 1)}
        elif t in by_id:
            chosen.add(t)
        elif low in by_name:
            chosen.add(by_name[low].id)
        else:
            raise TargetError(f"unknown target '{t}'")
    return [n for n in nodes if n.id in chosen]


def node_role(node: NodeView) -> str:
    """'ap' / 'cpe' / 'other' for devices, 'backbone' for backbones."""
    if isinstance(node.config, DeviceConfig):
        return node.config.role
    return node.kind
