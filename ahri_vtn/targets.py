"""
Target selections: any mix of whole VENs, individual resources and saved groups, resolved to
per-VEN OpenADR targets and to the list of units (VEN / resource pairs) that a scenario must check.

Wire format (JSON API, stored on dispatches and runs):

    {"vens": [{"ven_pk": 3}, {"ven_pk": 4, "resource_ids": ["hvac-1", "hvac-2"]}],
     "groups": [2]}

A ven entry without "resource_ids" (or with null) means the whole VEN. HTML forms send the same
information as repeated "target" values: "ven:3", "res:4:hvac-1", "group:2".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .db import models


@dataclass
class VenTarget:
    ven_pk: int
    ven_id: Optional[str]
    ven_name: str
    resource_ids: Optional[list] = None       # None = whole VEN
    group_ids: list = field(default_factory=list)   # groupID values to signal
    known_resource_ids: list = field(default_factory=list)

    @property
    def whole_ven(self) -> bool:
        return self.resource_ids is None

    def openadr_targets(self) -> list[dict]:
        """
        eiTarget entries for an event to this VEN. A groupID is only signalled for whole-VEN
        members: OpenADR group membership is a VEN-level property, and a VEN that matches a
        groupID applies the event to everything it controls, which would defeat a resource
        selection.
        """
        targets = []
        if self.whole_ven:
            targets.append({"ven_id": self.ven_id})
            targets.extend({"group_id": gid} for gid in self.group_ids)
        else:
            targets.extend({"resource_id": rid} for rid in self.resource_ids)
        return targets

    def units(self) -> list["Unit"]:
        """The units a scenario must check: the selected resources, all known resources of a whole
        VEN, or the VEN itself when it reports no resource ids."""
        rids = self.resource_ids if not self.whole_ven else self.known_resource_ids
        if not rids:
            return [Unit(self.ven_pk, self.ven_id, self.ven_name, None)]
        return [Unit(self.ven_pk, self.ven_id, self.ven_name, rid) for rid in rids]

    def describe(self) -> str:
        if self.whole_ven:
            return f"{self.ven_name} (whole VEN)"
        return f"{self.ven_name}: {', '.join(self.resource_ids)}"

    def as_dict(self) -> dict:
        return {"ven_pk": self.ven_pk, "ven_id": self.ven_id, "ven_name": self.ven_name,
                "resource_ids": self.resource_ids, "group_ids": self.group_ids}


@dataclass(frozen=True)
class Unit:
    ven_pk: int
    ven_id: Optional[str]
    ven_name: str
    resource_id: Optional[str]

    @property
    def label(self) -> str:
        return f"{self.ven_name}/{self.resource_id}" if self.resource_id else self.ven_name

    @property
    def key(self) -> str:
        return f"{self.ven_pk}:{self.resource_id or ''}"


class Selection:
    def __init__(self, vens: Optional[Iterable[dict]] = None, groups: Optional[Iterable[int]] = None):
        self.vens: dict[int, Optional[set]] = {}      # ven_pk -> None (whole) or set of resource ids
        self.groups: list[int] = []
        for entry in vens or []:
            self.add_ven(int(entry["ven_pk"]), entry.get("resource_ids"))
        for g in groups or []:
            if g is not None and int(g) not in self.groups:
                self.groups.append(int(g))

    # ---- building

    def add_ven(self, ven_pk: int, resource_ids: Optional[Iterable[str]] = None):
        if resource_ids is None:
            self.vens[ven_pk] = None
            return
        if ven_pk in self.vens and self.vens[ven_pk] is None:
            return                                   # already whole VEN
        self.vens.setdefault(ven_pk, set()).update(str(r) for r in resource_ids if r)
        if not self.vens[ven_pk]:
            self.vens[ven_pk] = None

    def add_group(self, group_pk: int):
        if int(group_pk) not in self.groups:
            self.groups.append(int(group_pk))

    @classmethod
    def from_form(cls, values: Iterable[str]) -> "Selection":
        """Parse repeated form values: ven:<pk>, res:<pk>:<resource_id>, group:<pk>."""
        sel = cls()
        for value in values:
            if not value:
                continue
            kind, _, rest = str(value).partition(":")
            if kind == "ven":
                sel.add_ven(int(rest))
            elif kind == "res":
                pk, _, rid = rest.partition(":")
                sel.add_ven(int(pk), [rid])
            elif kind == "group":
                sel.add_group(int(rest))
            else:
                raise ValueError(f"Unknown target '{value}'")
        return sel

    @classmethod
    def from_any(cls, data) -> "Selection":
        """Accept a Selection, the wire dict, a list of form values, or a bare ven_pk."""
        if isinstance(data, Selection):
            return data
        if data is None:
            return cls()
        if isinstance(data, dict):
            return cls(vens=data.get("vens"), groups=data.get("groups"))
        if isinstance(data, (list, tuple)):
            return cls.from_form(data)
        return cls(vens=[{"ven_pk": int(data)}])

    @classmethod
    def whole_ven(cls, ven_pk: int) -> "Selection":
        return cls(vens=[{"ven_pk": ven_pk}])

    @classmethod
    def resource(cls, ven_pk: int, resource_id: str) -> "Selection":
        return cls(vens=[{"ven_pk": ven_pk, "resource_ids": [resource_id]}])

    def is_empty(self) -> bool:
        return not self.vens and not self.groups

    def as_dict(self) -> dict:
        return {"vens": [{"ven_pk": pk, "resource_ids": sorted(rids) if rids is not None else None}
                         for pk, rids in sorted(self.vens.items())],
                "groups": list(self.groups)}

    def form_values(self) -> list[str]:
        values = []
        for pk, rids in self.vens.items():
            if rids is None:
                values.append(f"ven:{pk}")
            else:
                values.extend(f"res:{pk}:{rid}" for rid in sorted(rids))
        values.extend(f"group:{g}" for g in self.groups)
        return values

    # ---- resolving

    def resolve(self, db) -> list[VenTarget]:
        """Merge groups into the explicit selection and return one VenTarget per VEN."""
        merged: dict[int, Optional[set]] = {pk: (None if rids is None else set(rids)) for pk, rids in self.vens.items()}
        group_signals: dict[int, list] = {}
        with db.session() as s:
            for group_pk in self.groups:
                group = s.get(models.Group, group_pk)
                if group is None:
                    raise ValueError(f"Unknown group {group_pk}")
                for member in group.members:
                    if member.resource_id is None:
                        merged[member.ven_pk] = None
                    elif member.ven_pk not in merged or merged[member.ven_pk] is not None:
                        merged.setdefault(member.ven_pk, set()).add(member.resource_id)
                    if group.signal_group_id:
                        group_signals.setdefault(member.ven_pk, [])
                        if group.signal_group_id not in group_signals[member.ven_pk]:
                            group_signals[member.ven_pk].append(group.signal_group_id)
            targets = []
            for ven_pk in sorted(merged):
                ven = s.get(models.Ven, ven_pk)
                if ven is None:
                    raise ValueError(f"Unknown VEN {ven_pk}")
                known = sorted(r.resource_id for r in ven.resources)
                rids = merged[ven_pk]
                targets.append(VenTarget(ven_pk=ven.id, ven_id=ven.ven_id, ven_name=ven.ven_name,
                                         resource_ids=None if rids is None else sorted(rids),
                                         group_ids=group_signals.get(ven_pk, []) if rids is None else [],
                                         known_resource_ids=known))
            return targets

    def units(self, db) -> list[Unit]:
        result = []
        for target in self.resolve(db):
            result.extend(target.units())
        return result

    def describe(self, db) -> str:
        return "; ".join(t.describe() for t in self.resolve(db)) or "(nothing selected)"
