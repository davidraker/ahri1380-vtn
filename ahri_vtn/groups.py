"""Saved target groups (named sets of VENs and resources)."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from .db import models
from .targets import Selection


class GroupManager:
    def __init__(self, vtn):
        self.vtn = vtn

    def _group_dict(self, s, group: models.Group) -> dict:
        members = []
        for m in group.members:
            ven = s.get(models.Ven, m.ven_pk)
            members.append({"ven_pk": m.ven_pk, "ven_name": ven.ven_name if ven else "?", "resource_id": m.resource_id})
        selection = Selection()
        for m in group.members:
            selection.add_ven(m.ven_pk, None if m.resource_id is None else [m.resource_id])
        summary = "; ".join(f"{name} ({'whole VEN' if rid is None else rid})"
                            for name, rid in sorted((m["ven_name"], m["resource_id"] or "") for m in members)
                            for rid in [rid or None]) or "(empty)"
        return {**group.as_dict(), "members": members, "selection": selection.as_dict(),
                "form_values": selection.form_values(), "summary": summary}

    def list_groups(self) -> list[dict]:
        with self.vtn.db.session() as s:
            return [self._group_dict(s, g) for g in s.scalars(select(models.Group).order_by(models.Group.name))]

    def get_group(self, group_pk: int) -> Optional[dict]:
        with self.vtn.db.session() as s:
            g = s.get(models.Group, group_pk)
            return self._group_dict(s, g) if g else None

    def save_group(self, name: str, selection, description: Optional[str] = None,
                   signal_group_id: Optional[str] = None, group_pk: Optional[int] = None) -> dict:
        """Create (group_pk None) or replace the definition of a group. Groups may not contain groups."""
        name = (name or "").strip()
        if not name:
            raise ValueError("A group needs a name")
        sel = Selection.from_any(selection)
        if sel.groups:
            raise ValueError("Groups cannot contain other groups; select VENs and resources")
        if sel.is_empty():
            raise ValueError("Select at least one VEN or resource for the group")
        with self.vtn.db.session() as s:
            existing = s.scalar(select(models.Group).where(models.Group.name == name))
            if group_pk is None:
                if existing:
                    raise ValueError(f"A group named '{name}' already exists")
                group = models.Group(name=name)
                s.add(group)
                s.flush()
            else:
                group = s.get(models.Group, group_pk)
                if group is None:
                    raise ValueError("Unknown group")
                if existing and existing.id != group.id:
                    raise ValueError(f"A group named '{name}' already exists")
                group.name = name
                group.members.clear()
            group.description = description or None
            group.signal_group_id = (signal_group_id or "").strip() or None
            for ven_pk, rids in sel.vens.items():
                if s.get(models.Ven, ven_pk) is None:
                    raise ValueError(f"Unknown VEN {ven_pk}")
                if rids is None:
                    group.members.append(models.GroupMember(ven_pk=ven_pk, resource_id=None))
                else:
                    for rid in sorted(rids):
                        group.members.append(models.GroupMember(ven_pk=ven_pk, resource_id=rid))
            s.flush()
            result = self._group_dict(s, group)
        self.vtn.bus.publish("group.saved", group_pk=result["id"], name=name)
        return result

    def delete_group(self, group_pk: int) -> bool:
        with self.vtn.db.session() as s:
            g = s.get(models.Group, group_pk)
            if g is None:
                return False
            s.delete(g)
        self.vtn.bus.publish("group.deleted", group_pk=group_pk)
        return True
