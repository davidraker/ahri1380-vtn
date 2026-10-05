"""
Event management: building AHRI 1380 events (Table 8), cancelling them, recording VEN
responses (oadrCreatedEvent) and customer overrides (oadrCreateOpt).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Optional

from openleadr import enums, objects, utils as oadr_utils
from sqlalchemy import select

from .. import spec
from ..db import models
from ..util import jsonable, utcnow

if TYPE_CHECKING:
    from ..server import AhriVtn

logger = logging.getLogger("ahri_vtn.events")

ACTIVE_STATUSES = ("far", "near", "active")


class EventManager:
    def __init__(self, vtn: "AhriVtn"):
        self.vtn = vtn

    # ------------------------------------------------------------------ building events

    def create_event(self, ven_pk: int, preset_id: str, *, start: Optional[datetime] = None,
                     duration: Optional[timedelta] = None, payload: Optional[float] = None,
                     mito: Optional[int] = None, price_multipliers: Optional[list] = None,
                     price_interval: timedelta = timedelta(hours=1), test_event: bool = False,
                     vtn_comment: Optional[str] = None, ramp_up: Optional[timedelta] = None,
                     notification: Optional[timedelta] = None, response_required: str = "always",
                     priority: int = 0, source: str = "operator", test_step_pk: Optional[int] = None,
                     label: Optional[str] = None) -> str:
        """
        Create and queue an event for a VEN. Returns the event_id.

        preset_id is one of spec.EVENT_PRESETS. For SIMPLE presets an optional `mito` adds a second
        LOAD_CONTROL:x-loadControlLevelOffset signal (Table 8 "if the event includes two signals").
        For 'price_stream', `price_multipliers` is a list of multipliers, one per `price_interval`.
        """
        preset = spec.EVENT_PRESETS[preset_id]
        start = start or utcnow()
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        duration = duration or timedelta(minutes=self.vtn.config.default_event_duration_minutes)
        if preset.payload is not None and payload is None:
            payload = preset.payload
        with self.vtn.db.session() as s:
            ven = s.get(models.Ven, ven_pk)
            if ven is None or not ven.ven_id:
                raise ValueError("Unknown or unregistered VEN")
            ven_id = ven.ven_id

        signals = []
        if preset.signal_name == "ELECTRICITY_PRICE":
            if not price_multipliers:
                raise ValueError("price_stream needs a list of price multipliers")
            intervals = []
            t = start
            for multiplier in price_multipliers:
                intervals.append(objects.Interval(dtstart=t, duration=price_interval, signal_payload=float(multiplier)))
                t += price_interval
            duration = price_interval * len(price_multipliers)
            signals.append(objects.EventSignal(intervals=intervals, signal_name=preset.signal_name,
                                               signal_type=preset.signal_type, signal_id=oadr_utils.generate_id()))
        else:
            if payload is None:
                raise ValueError(f"{preset.label} needs a payload value")
            if preset.payload_min is not None and payload < preset.payload_min \
                    or preset.payload_max is not None and payload > preset.payload_max:
                raise ValueError(f"{preset.payload_label} must be between {preset.payload_min} and {preset.payload_max}")
            if preset.signal_name == "SIMPLE":
                payload = int(payload)
            signals.append(objects.EventSignal(
                intervals=[objects.Interval(dtstart=start, duration=duration, signal_payload=payload)],
                signal_name=preset.signal_name, signal_type=preset.signal_type, signal_id=oadr_utils.generate_id()))
            if mito is not None and preset.signal_name == "SIMPLE":
                signals.append(objects.EventSignal(
                    intervals=[objects.Interval(dtstart=start, duration=duration, signal_payload=int(mito))],
                    signal_name="LOAD_CONTROL", signal_type=enums.SIGNAL_TYPE.X_LOAD_CONTROL_LEVEL_OFFSET,
                    signal_id=oadr_utils.generate_id()))

        event_id = oadr_utils.generate_id()
        descriptor = objects.EventDescriptor(event_id=event_id, modification_number=0,
                                             market_context=self.vtn.config.market_context, event_status="far",
                                             priority=priority, test_event=test_event, vtn_comment=vtn_comment,
                                             created_date_time=utcnow())
        active_period = objects.ActivePeriod(dtstart=start, duration=duration, ramp_up_period=ramp_up,
                                             notification_period=notification)
        event = objects.Event(event_descriptor=descriptor, event_signals=signals, targets=[{"ven_id": ven_id}],
                              active_period=active_period, response_required=response_required)

        with self.vtn.db.session() as s:
            s.add(models.Event(
                event_id=event_id, ven_pk=ven_pk, preset=preset_id, label=label or preset.label,
                market_context=self.vtn.config.market_context, status=descriptor.event_status,
                modification_number=0, test_event=test_event, vtn_comment=vtn_comment, priority=priority,
                dtstart=start, duration_seconds=duration.total_seconds(),
                ramp_up_seconds=ramp_up.total_seconds() if ramp_up else None,
                notification_seconds=notification.total_seconds() if notification else None,
                signals=jsonable([{"signal_name": sg.signal_name, "signal_type": sg.signal_type,
                                   "signal_id": sg.signal_id,
                                   "intervals": [{"dtstart": i.dtstart, "duration": i.duration,
                                                  "payload": i.signal_payload} for i in sg.intervals]}
                                  for sg in signals]),
                source=source, test_step_pk=test_step_pk))

        self.vtn.server.add_raw_event(ven_id=ven_id, event=event, callback=self._on_event_response,
                                      delivery_callback=partial(self._on_event_delivered, event_id))
        self.vtn.bus.publish("event.created", ven_pk=ven_pk, ven_id=ven_id, event_id=event_id, preset=preset_id,
                             dtstart=start.isoformat(), duration=duration.total_seconds())
        logger.info(f"Queued event {event_id} ({preset.label}) for VEN {ven_id}")
        return event_id

    def cancel_event(self, event_id: str) -> bool:
        with self.vtn.db.session() as s:
            ev = s.scalar(select(models.Event).where(models.Event.event_id == event_id))
            if ev is None:
                return False
            ven = s.get(models.Ven, ev.ven_pk)
            if ev.status in ("cancelled", "completed"):
                return False
            ev.status = "cancelled"
            ev.modification_number += 1
            ev.cancelled_at = utcnow()
            ven_id, ven_pk = ven.ven_id, ven.id
        self.vtn.server.cancel_event(ven_id, event_id)
        self.vtn.bus.publish("event.cancelled", ven_pk=ven_pk, ven_id=ven_id, event_id=event_id)
        logger.info(f"Cancelled event {event_id} for VEN {ven_id}")
        return True

    # ------------------------------------------------------------------ callbacks from OpenLEADR

    async def _on_event_delivered(self, event_id):
        with self.vtn.db.session() as s:
            ev = s.scalar(select(models.Event).where(models.Event.event_id == event_id))
            ven_pk = ev.ven_pk if ev else None
        self.vtn.bus.publish("event.delivered", event_id=event_id, ven_pk=ven_pk)

    async def _on_event_response(self, ven_id, event_id, opt_type, response_code=None,
                                 response_description=None, modification_number=None, request_id=None):
        now = utcnow()
        with self.vtn.db.session() as s:
            ev = s.scalar(select(models.Event).where(models.Event.event_id == event_id))
            ven = s.scalar(select(models.Ven).where(models.Ven.ven_id == ven_id))
            s.add(models.EventResponse(event_pk=ev.id if ev else None, ven_pk=ven.id if ven else None,
                                       event_id=event_id, modification_number=modification_number,
                                       opt_type=opt_type, response_code=response_code,
                                       response_description=response_description, request_id=request_id,
                                       received_at=now))
            if ven is not None:
                ven.last_seen_at = now
            ven_pk = ven.id if ven else None
        self.vtn.bus.publish("event.response", ven_pk=ven_pk, ven_id=ven_id, event_id=event_id, opt_type=opt_type,
                             response_code=response_code, response_description=response_description,
                             modification_number=modification_number)
        logger.info(f"VEN {ven_id} responded {opt_type} ({response_code}) to event {event_id} "
                    f"modification {modification_number}")

    async def on_create_opt(self, payload):
        ven_id = payload.get("ven_id")
        now = utcnow()
        with self.vtn.db.session() as s:
            ven = s.scalar(select(models.Ven).where(models.Ven.ven_id == ven_id))
            s.add(models.Opt(opt_id=str(payload.get("opt_id")), ven_pk=ven.id if ven else None,
                             opt_type=payload.get("opt_type"), opt_reason=payload.get("opt_reason"),
                             event_id=payload.get("event_id"), modification_number=payload.get("modification_number"),
                             market_context=payload.get("market_context"), targets=jsonable(payload.get("targets")),
                             created_date_time=payload.get("created_date_time"), received_at=now,
                             raw=jsonable(payload)))
            if ven is not None:
                ven.last_seen_at = now
            ven_pk = ven.id if ven else None
        self.vtn.bus.publish("opt.created", ven_pk=ven_pk, ven_id=ven_id, opt_id=payload.get("opt_id"),
                             opt_type=payload.get("opt_type"), opt_reason=payload.get("opt_reason"),
                             event_id=payload.get("event_id"))
        logger.info(f"VEN {ven_id} sent opt {payload.get('opt_type')}/{payload.get('opt_reason')} "
                    f"for event {payload.get('event_id')}")

    async def on_cancel_opt(self, payload):
        ven_id = payload.get("ven_id")
        with self.vtn.db.session() as s:
            ven = s.scalar(select(models.Ven).where(models.Ven.ven_id == ven_id))
            query = select(models.Opt).where(models.Opt.opt_id == str(payload.get("opt_id")))
            if ven is not None:
                query = query.where(models.Opt.ven_pk == ven.id)
            for opt in s.scalars(query):
                opt.cancelled_at = utcnow()
            ven_pk = ven.id if ven else None
        self.vtn.bus.publish("opt.cancelled", ven_pk=ven_pk, ven_id=ven_id, opt_id=payload.get("opt_id"))

    # ------------------------------------------------------------------ housekeeping

    def refresh_event_statuses(self):
        """Recompute far/near/active/completed for live events (called periodically)."""
        changed = []
        with self.vtn.db.session() as s:
            for ev in s.scalars(select(models.Event).where(models.Event.status.in_(ACTIVE_STATUSES))):
                active_period = {"dtstart": ev.dtstart, "duration": timedelta(seconds=ev.duration_seconds),
                                 "ramp_up_period": timedelta(seconds=ev.ramp_up_seconds) if ev.ramp_up_seconds else None}
                status = oadr_utils.determine_event_status(active_period)
                if status != ev.status:
                    ev.status = status
                    if status == "completed":
                        ev.completed_at = utcnow()
                    changed.append((ev.event_id, status, ev.ven_pk))
        for event_id, status, ven_pk in changed:
            self.vtn.bus.publish("event.status", event_id=event_id, status=status, ven_pk=ven_pk)
        return changed
