"""
Database models for the AHRI 1380 VTN test tool (SQLAlchemy 2.0 declarative).
All timestamps are stored as UTC and returned timezone-aware.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        TypeDecorator)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Store aware datetimes as UTC and always return aware UTC datetimes (SQLite drops tzinfo)."""
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime, dict: JSON, list: JSON}

    def as_dict(self) -> dict:
        out = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            out[column.name] = value
        return out


# ---------------------------------------------------------------------------------------------
# VENs and registration
# ---------------------------------------------------------------------------------------------

class Ven(Base):
    __tablename__ = "vens"
    id: Mapped[int] = mapped_column(primary_key=True)
    ven_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    ven_id: Mapped[Optional[str]] = mapped_column(String(128), unique=True, index=True)
    registration_id: Mapped[Optional[str]] = mapped_column(String(128))
    fingerprint: Mapped[Optional[str]] = mapped_column(String(128))
    profile_name: Mapped[Optional[str]] = mapped_column(String(16))
    transport_name: Mapped[Optional[str]] = mapped_column(String(32))
    transport_address: Mapped[Optional[str]] = mapped_column(String(255))
    http_pull_model: Mapped[Optional[bool]] = mapped_column(Boolean)
    xml_signature: Mapped[Optional[bool]] = mapped_column(Boolean)
    report_only: Mapped[Optional[bool]] = mapped_column(Boolean)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    registered: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    first_registered_at: Mapped[Optional[datetime]] = mapped_column()
    last_registered_at: Mapped[Optional[datetime]] = mapped_column()
    last_seen_at: Mapped[Optional[datetime]] = mapped_column()
    last_poll_at: Mapped[Optional[datetime]] = mapped_column()
    poll_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_status: Mapped[Optional[dict]] = mapped_column(JSON)   # {'Inst Power': {...}, ...}
    static_info: Mapped[Optional[dict]] = mapped_column(JSON)     # Table 5 values keyed by canonical rID

    registrations: Mapped[list["RegistrationEvent"]] = relationship(back_populates="ven", cascade="all, delete-orphan")
    registered_reports: Mapped[list["RegisteredReport"]] = relationship(back_populates="ven", cascade="all, delete-orphan")
    resources: Mapped[list["Resource"]] = relationship(back_populates="ven", cascade="all, delete-orphan")


class Resource(Base):
    """A resource (HVAC unit) behind a VEN, discovered from the reportDataSource/resourceID of its reports."""
    __tablename__ = "resources"
    __table_args__ = (Index("ix_resources_ven_rid", "ven_pk", "resource_id", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ven_pk: Mapped[int] = mapped_column(ForeignKey("vens.id"), index=True)
    resource_id: Mapped[str] = mapped_column(String(255))
    label: Mapped[Optional[str]] = mapped_column(String(255))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column()
    latest_status: Mapped[Optional[dict]] = mapped_column(JSON)
    static_info: Mapped[Optional[dict]] = mapped_column(JSON)
    ven: Mapped["Ven"] = relationship(back_populates="resources")


class Group(Base):
    """A named, saved set of VENs and resources that can be targeted by events, report requests and runs."""
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    signal_group_id: Mapped[Optional[str]] = mapped_column(String(255))   # eiTarget/groupID to send, if any
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    members: Mapped[list["GroupMember"]] = relationship(back_populates="group", cascade="all, delete-orphan")


class GroupMember(Base):
    __tablename__ = "group_members"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_pk: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    ven_pk: Mapped[int] = mapped_column(ForeignKey("vens.id", ondelete="CASCADE"), index=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(255))     # None = the whole VEN
    group: Mapped["Group"] = relationship(back_populates="members")


class Dispatch(Base):
    """One operator / scenario action that produced events (or report requests) for a selection of targets."""
    __tablename__ = "dispatches"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))                        # event | report_request
    label: Mapped[Optional[str]] = mapped_column(String(255))
    preset: Mapped[Optional[str]] = mapped_column(String(64))           # event preset or report spec name
    params: Mapped[Optional[dict]] = mapped_column(JSON)
    selection: Mapped[Optional[dict]] = mapped_column(JSON)             # the selection as requested
    resolved: Mapped[Optional[list]] = mapped_column(JSON)              # per-VEN targets actually used
    source: Mapped[str] = mapped_column(String(32), default="operator")
    test_step_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("test_steps.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column()
    events: Mapped[list["Event"]] = relationship(back_populates="dispatch")


class RegistrationEvent(Base):
    __tablename__ = "registration_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ven_pk: Mapped[int] = mapped_column(ForeignKey("vens.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))      # create | reregister | cancel | query | request_reregistration
    accepted: Mapped[Optional[bool]] = mapped_column(Boolean)
    details: Mapped[Optional[dict]] = mapped_column(JSON)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    ven: Mapped["Ven"] = relationship(back_populates="registrations")


# ---------------------------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------------------------

class RegisteredReport(Base):
    __tablename__ = "registered_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    ven_pk: Mapped[int] = mapped_column(ForeignKey("vens.id"), index=True)
    report_name_raw: Mapped[Optional[str]] = mapped_column(String(128))
    report_name: Mapped[Optional[str]] = mapped_column(String(128))      # METADATA prefix stripped
    spec_name: Mapped[Optional[str]] = mapped_column(String(64))         # x-AHRI_STATIC / x-AHRI_STATUS / None
    report_specifier_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    created_date_time: Mapped[Optional[datetime]] = mapped_column()
    registered_at: Mapped[datetime] = mapped_column(default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw: Mapped[Optional[dict]] = mapped_column(JSON)
    ven: Mapped["Ven"] = relationship(back_populates="registered_reports")
    descriptions: Mapped[list["ReportDescription"]] = relationship(back_populates="report", cascade="all, delete-orphan")


class ReportDescription(Base):
    __tablename__ = "report_descriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_pk: Mapped[int] = mapped_column(ForeignKey("registered_reports.id"), index=True)
    r_id: Mapped[str] = mapped_column(String(255))
    r_key: Mapped[str] = mapped_column(String(255), index=True)
    spec_rid: Mapped[Optional[str]] = mapped_column(String(64))
    report_type: Mapped[Optional[str]] = mapped_column(String(64))
    reading_type: Mapped[Optional[str]] = mapped_column(String(64))
    measurement_name: Mapped[Optional[str]] = mapped_column(String(64))
    measurement_description: Mapped[Optional[str]] = mapped_column(String(255))
    measurement_unit: Mapped[Optional[str]] = mapped_column(String(64))
    measurement_scale: Mapped[Optional[str]] = mapped_column(String(16))
    min_period_seconds: Mapped[Optional[float]] = mapped_column(Float)
    max_period_seconds: Mapped[Optional[float]] = mapped_column(Float)
    on_change: Mapped[Optional[bool]] = mapped_column(Boolean)
    resource_id: Mapped[Optional[str]] = mapped_column(String(255))
    market_context: Mapped[Optional[str]] = mapped_column(String(255))
    report: Mapped["RegisteredReport"] = relationship(back_populates="descriptions")


class ReportRequest(Base):
    __tablename__ = "report_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_request_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    ven_pk: Mapped[int] = mapped_column(ForeignKey("vens.id"), index=True)
    report_specifier_id: Mapped[Optional[str]] = mapped_column(String(255))
    spec_name: Mapped[Optional[str]] = mapped_column(String(64))
    r_ids: Mapped[Optional[list]] = mapped_column(JSON)
    granularity_seconds: Mapped[Optional[float]] = mapped_column(Float)
    report_back_duration_seconds: Mapped[Optional[float]] = mapped_column(Float)
    resource_id: Mapped[Optional[str]] = mapped_column(String(255))     # when the report covers a single resource
    dispatch_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("dispatches.id"), index=True)
    one_shot: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="queued")   # queued|created|active|cancel_requested|cancelled|completed
    source: Mapped[str] = mapped_column(String(32), default="auto")     # auto|operator|scenario
    requested_at: Mapped[datetime] = mapped_column(default=utcnow)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column()
    cancelled_at: Mapped[Optional[datetime]] = mapped_column()
    last_update_at: Mapped[Optional[datetime]] = mapped_column()
    update_count: Mapped[int] = mapped_column(Integer, default=0)
    test_step_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("test_steps.id"))


class ReportSample(Base):
    __tablename__ = "report_samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    ven_pk: Mapped[int] = mapped_column(ForeignKey("vens.id"), index=True)
    report_request_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    report_specifier_id: Mapped[Optional[str]] = mapped_column(String(255))
    spec_name: Mapped[Optional[str]] = mapped_column(String(64))
    resource_id: Mapped[Optional[str]] = mapped_column(String(255))
    r_id: Mapped[str] = mapped_column(String(255))
    r_key: Mapped[str] = mapped_column(String(255))
    spec_rid: Mapped[Optional[str]] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column()                      # interval dtstart reported by the VEN
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
    value_float: Mapped[Optional[float]] = mapped_column(Float)
    value_text: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    accuracy: Mapped[Optional[float]] = mapped_column(Float)
    data_quality: Mapped[Optional[str]] = mapped_column(String(64))
    extra: Mapped[Optional[dict]] = mapped_column(JSON)
    message_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id"))

    __table_args__ = (Index("ix_samples_ven_rkey_ts", "ven_pk", "r_key", "ts"),)

    @property
    def value(self):
        return self.value_float if self.value_float is not None else self.value_text


# ---------------------------------------------------------------------------------------------
# Events and opts
# ---------------------------------------------------------------------------------------------

class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    ven_pk: Mapped[int] = mapped_column(ForeignKey("vens.id"), index=True)
    preset: Mapped[Optional[str]] = mapped_column(String(64))
    label: Mapped[Optional[str]] = mapped_column(String(255))
    market_context: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="far")   # far|near|active|completed|cancelled
    modification_number: Mapped[int] = mapped_column(Integer, default=0)
    test_event: Mapped[bool] = mapped_column(Boolean, default=False)
    vtn_comment: Mapped[Optional[str]] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    dtstart: Mapped[datetime] = mapped_column()
    duration_seconds: Mapped[float] = mapped_column(Float)
    ramp_up_seconds: Mapped[Optional[float]] = mapped_column(Float)
    notification_seconds: Mapped[Optional[float]] = mapped_column(Float)
    signals: Mapped[Optional[list]] = mapped_column(JSON)
    targets: Mapped[Optional[list]] = mapped_column(JSON)               # eiTarget entries as sent
    target_resource_ids: Mapped[Optional[list]] = mapped_column(JSON)   # None = the whole VEN
    dispatch_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("dispatches.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column()
    completed_at: Mapped[Optional[datetime]] = mapped_column()
    source: Mapped[str] = mapped_column(String(32), default="operator")
    test_step_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("test_steps.id"))
    responses: Mapped[list["EventResponse"]] = relationship(back_populates="event", cascade="all, delete-orphan")
    dispatch: Mapped[Optional["Dispatch"]] = relationship(back_populates="events")

    @property
    def dtend(self) -> datetime:
        from datetime import timedelta
        return self.dtstart + timedelta(seconds=self.duration_seconds)


class EventResponse(Base):
    __tablename__ = "event_responses"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("events.id"), index=True)
    ven_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("vens.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(128), index=True)
    modification_number: Mapped[Optional[int]] = mapped_column(Integer)
    opt_type: Mapped[Optional[str]] = mapped_column(String(16))
    response_code: Mapped[Optional[int]] = mapped_column(Integer)
    response_description: Mapped[Optional[str]] = mapped_column(String(255))
    request_id: Mapped[Optional[str]] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
    event: Mapped[Optional["Event"]] = relationship(back_populates="responses")


class Opt(Base):
    __tablename__ = "opts"
    id: Mapped[int] = mapped_column(primary_key=True)
    opt_id: Mapped[str] = mapped_column(String(128), index=True)
    ven_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("vens.id"), index=True)
    opt_type: Mapped[Optional[str]] = mapped_column(String(16))
    opt_reason: Mapped[Optional[str]] = mapped_column(String(64))
    event_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    modification_number: Mapped[Optional[int]] = mapped_column(Integer)
    market_context: Mapped[Optional[str]] = mapped_column(String(255))
    targets: Mapped[Optional[list]] = mapped_column(JSON)
    resource_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)   # first resourceID target, if any
    created_date_time: Mapped[Optional[datetime]] = mapped_column()
    received_at: Mapped[datetime] = mapped_column(default=utcnow)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column()
    raw: Mapped[Optional[dict]] = mapped_column(JSON)


# ---------------------------------------------------------------------------------------------
# Raw message log
# ---------------------------------------------------------------------------------------------

class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    direction: Mapped[str] = mapped_column(String(4))               # in | out
    message_type: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    ven_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    remote: Mapped[Optional[str]] = mapped_column(String(64))
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    schema_error: Mapped[Optional[str]] = mapped_column(Text)
    raw_xml: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    in_reply_to: Mapped[Optional[str]] = mapped_column(String(64))
    pair_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)


# ---------------------------------------------------------------------------------------------
# Conformance runs
# ---------------------------------------------------------------------------------------------

class TestRun(Base):
    __tablename__ = "test_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    ven_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("vens.id"), index=True)
    selection: Mapped[Optional[dict]] = mapped_column(JSON)             # targets as requested
    resolved: Mapped[Optional[list]] = mapped_column(JSON)              # per-VEN targets at start
    mode: Mapped[str] = mapped_column(String(16), default="cooling")
    params: Mapped[Optional[dict]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="running")  # running|passed|failed|needs_review|aborted|error
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column()
    summary: Mapped[Optional[dict]] = mapped_column(JSON)
    operator_notes: Mapped[Optional[str]] = mapped_column(Text)
    steps: Mapped[list["TestStep"]] = relationship(back_populates="run", cascade="all, delete-orphan",
                                                   order_by="TestStep.index")


class TestStep(Base):
    __tablename__ = "test_steps"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_pk: Mapped[int] = mapped_column(ForeignKey("test_runs.id"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|passed|failed|manual|info|skipped|waiting
    started_at: Mapped[Optional[datetime]] = mapped_column()
    finished_at: Mapped[Optional[datetime]] = mapped_column()
    details: Mapped[Optional[dict]] = mapped_column(JSON)      # {'assertions': [...], 'prompt': ...}
    run: Mapped["TestRun"] = relationship(back_populates="steps")


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[int] = mapped_column(primary_key=True)
    ven_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("vens.id"), index=True)
    run_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("test_runs.id"), index=True)
    step_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("test_steps.id"))
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    severity: Mapped[str] = mapped_column(String(16))          # info | warning | fail | pass
    code: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    evidence: Mapped[Optional[dict]] = mapped_column(JSON)
