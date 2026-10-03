"""SQLAlchemy ORM models.

History is event-sourced:

* ``applications``       - every submitted request, rejected submissions are
                           never persisted (see whole-request validation);
* ``approval_events``    - append-only approval record, one per revision
                           (``auto_approved``/``release_revision`` mark
                           approvals granted automatically from the waitlist
                           during a suspend/revoke release);
* ``suspension_events``  - append-only record of a temporary outage: an
                           approved license is taken out of the effective set,
                           one per revision;
* ``resumption_events``  - append-only record restoring a suspended license
                           after re-adjudication, one per revision (keeps the
                           guard band snapshot the re-decision used);
* ``revocation_events``  - append-only revocation record, one per revision
                           (a suspended license may be permanently revoked);
* ``license_transfer_events`` - append-only equipment-change handover record,
                           one per handover, keyed by the revocation revision;
                           links the ordinary revocation of the source license
                           and the ordinary (manual) approval of its pending
                           successor, which keep two consecutive revisions;
* ``guard_band_events``  - append-only guard-band record, one per revision
                           (revision 0 is the initial value seeded at database
                           creation, every later row is one confirmed online
                           change);
* ``waitlist_entries``    - FIFO queue of pending applications registered as
                           standbys while the band is busy; registration and
                           cancellation consume no revision and the entry is
                           removed on approval (manual or automatic);
* ``restriction_windows`` - currently active temporary spectrum-control
                           windows (confirmed but not yet released); each
                           confirmation/release consumes one revision;
* ``restriction_window_events`` - append-only activation/release records for
                           restriction windows, one per revision, so snapshots
                           reconstruct the windows active at any revision;
* ``revision_meta``      - singleton row holding the current revision number
                           and the guard band currently in force.

A revocation never deletes history: it only removes an application from the
effective set used by later adjudications.  A suspension does the same
temporarily until a resumption puts the license back.  Snapshots for any past
revision are reconstructed from the event tables, including the guard band in
force at that revision.  Approval events keep their own immutable copy of the
band they were decided under and are never rewritten; resumption events keep
the band of their own re-adjudication.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Coverage circle in planar coordinates.
    cx: Mapped[float] = mapped_column(Float, nullable=False)
    cy: Mapped[float] = mapped_column(Float, nullable=False)
    radius: Mapped[float] = mapped_column(Float, nullable=False)

    # Closed frequency interval [freq_low, freq_high].
    freq_low: Mapped[float] = mapped_column(Float, nullable=False)
    freq_high: Mapped[float] = mapped_column(Float, nullable=False)

    # UTC half-open interval [start_time, end_time).
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # pending -> approved -> suspended -> approved -> ... -> revoked
    # (suspension is temporary equipment downtime; revoked is terminal)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    approval_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Revision of the latest suspension while status == "suspended"; cleared
    # again on every resumption and left set as a historical marker on revoke.
    suspended_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ApprovalEvent(Base):
    __tablename__ = "approval_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    # Snapshot of the guard band in force when the decision was made.
    guard_band: Mapped[float] = mapped_column(Float, nullable=False)
    # True when the approval was granted automatically from the waitlist
    # while a suspension/revoke transaction was draining the queue.  Manual
    # approvals leave this false; original approval events are otherwise
    # identical and never rewritten.
    auto_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # For an auto approval: the single revision consumed by the suspend/
    # revoke release that triggered the queue drain (NULL for manual and
    # batch approvals).  Lets historical snapshots restore which automatic
    # decision each revision belonged to.
    release_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_approval_events_application", "application_id"),)


class RevocationEvent(Base):
    __tablename__ = "revocation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_revocation_events_application", "application_id"),)


class LicenseTransferEvent(Base):
    """Append-only record of an equipment-change license handover.

    A handover permanently revokes one currently effective license (the
    active-device license being handed in) and approves one still-pending
    application (the replacement device) as one atomic, conditional
    transaction.  The source revocation and the successor approval are the
    ordinary :class:`RevocationEvent` / :class:`ApprovalEvent` rows on two
    consecutive revisions (existing revoke/approve semantics are kept); this
    row only links them for audit.

    ``revision`` is the revocation revision; ``successor_approval_revision``
    is the next consecutive revision (both unique).  ``reason`` is the
    operator reason given for the handover.  A successor that was queued as a
    standby leaves the waitlist before its approval; afterwards the queue is
    re-adjudicated by the normal FIFO rules, exactly like after an ordinary
    revoke.  History snapshots replay this row alongside the ordinary
    events, so every revision of a handover (and the auto approvals it may
    trigger) reconstructs exactly as it was; original approval events are
    never rewritten.
    """

    __tablename__ = "license_transfer_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The handover's first revision: the source license revocation.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    source_application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    successor_application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    # The handover's second revision: the successor's (manual) approval.
    successor_approval_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, unique=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("ix_license_transfer_events_source", "source_application_id"),
        Index("ix_license_transfer_events_successor", "successor_application_id"),
    )


class SuspensionEvent(Base):
    """Append-only suspension record, one per revision.

    A suspension marks a temporary equipment outage: the license leaves the
    effective set immediately, keeps its original approval, and may later be
    resumed or permanently revoked.  Shares the revision sequence with
    approvals, resumptions, revocations and guard-band changes.
    """

    __tablename__ = "suspension_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_suspension_events_application", "application_id"),)


class ResumptionEvent(Base):
    """Append-only resumption record, one per revision.

    Restores a suspended license after re-adjudicating it against the current
    guard band and every effective license.  Carries the band snapshot the
    re-decision used, mirroring ``ApprovalEvent``; the original approval event
    is never rewritten.
    """

    __tablename__ = "resumption_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    # Snapshot of the guard band in force when the license was resumed.
    guard_band: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_resumption_events_application", "application_id"),)


class GuardBandEvent(Base):
    """Append-only guard-band record, one per revision.

    The row at revision 0 is the initial value seeded when the database was
    created (``previous_guard_band`` is NULL there); every later row is one
    confirmed online change and consumes exactly one revision, sharing the
    revision sequence with approvals and revocations.
    """

    __tablename__ = "guard_band_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    previous_guard_band: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    guard_band: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class RestrictionWindow(Base):
    """A currently active temporary spectrum-control window.

    Operators preview (a read-only rehearsal) and then confirm a window:
    coverage circle, closed frequency band, half-open UTC time interval,
    reason and the expected revision.  Confirmation consumes exactly one
    revision and inserts the window; releasing it with its window id and the
    expected revision consumes another.  Releasing only affects later
    adjudication and never auto-approves waitlist entries.

    Existing effective licenses stay effective when a window appears; from
    the confirmation revision onwards every pending preview, single/batch
    approval, suspended-license resumption and post-release waitlist
    auto-approval reports hit windows and no write may make a hit
    application effective.  A hit requires the circle, the half-open time
    interval and the closed frequency band all to intersect (touching band
    endpoints hit); the guard band never extends a window.

    This table mirrors the mutable current state; the append-only
    :class:`RestrictionWindowEvent` rows are the source of truth for
    historical revision snapshots.  ``start_time``/``end_time`` and the
    geometry are NULL on a release row (which only carries window/revision).
    """

    __tablename__ = "restriction_windows"

    # Stable window id assigned at confirmation; reused by the release call.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Revision at which the window was confirmed (activated).
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    cx: Mapped[float] = mapped_column(Float, nullable=False)
    cy: Mapped[float] = mapped_column(Float, nullable=False)
    radius: Mapped[float] = mapped_column(Float, nullable=False)
    freq_low: Mapped[float] = mapped_column(Float, nullable=False)
    freq_high: Mapped[float] = mapped_column(Float, nullable=False)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class RestrictionWindowEvent(Base):
    """Append-only lifecycle record of a restriction window, one per revision.

    ``event_type`` is ``"activated"`` (confirmation) or ``"released"``.
    Activation rows carry the full geometry, band, half-open interval and
    reason; release rows carry only the window id.  Historical revision
    snapshots replay these rows to reconstruct the windows active at any
    revision; original approval events are never rewritten.
    """

    __tablename__ = "restriction_window_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    # NULL on release rows; the activated window's id otherwise.
    window_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    cx: Mapped[float | None] = mapped_column(Float, nullable=True)
    cy: Mapped[float | None] = mapped_column(Float, nullable=True)
    radius: Mapped[float | None] = mapped_column(Float, nullable=True)
    freq_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    freq_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class WaitlistEntry(Base):
    """A pending application registered as a standby (FIFO queue).

    Only ``pending`` applications can be queued and each application can be
    queued at most once (unique constraint).  Registration and cancellation
    consume **no** revision: the queue is ordered strictly by insertion (the
    primary key doubles as the queue sequence number).  A successful manual
    or batch approval removes the entry (a manually approved application must
    never be auto-approved afterwards).  A suspension or revoke that releases
    an effective license drains the queue in registration order inside the
    same transaction: entries that pass re-adjudication under the live guard
    band are approved automatically, the rest keep their place and the drain
    continues with the following entries.  An entry hit by an active
    restriction window is blocked just like a license conflict: it keeps its
    place and the drain continues with the following entries.
    """

    __tablename__ = "waitlist_entries"

    # Monotonic queue sequence: registration order is the primary key order.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False, unique=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class RevisionMeta(Base):
    """Singleton row (id == 1): current revision + live guard band."""

    __tablename__ = "revision_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Live guard band used by every new adjudication.  Seeded once from the
    # GUARD_BAND environment variable; afterwards only changed through the
    # online config endpoint and never overwritten at startup.
    guard_band: Mapped[float] = mapped_column(Float, nullable=False)
