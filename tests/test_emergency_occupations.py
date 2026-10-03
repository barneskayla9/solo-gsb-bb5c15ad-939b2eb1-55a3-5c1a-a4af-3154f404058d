"""End-to-end tests for burst-communication emergency frequency occupations.

An emergency occupation pauses every *ordinary* effective license that
interferes with a pending emergency application (forced suspensions, no
waitlist re-adjudication, consecutive revisions), removes a queued
emergency application from the waitlist, and then approves it on the next
revision.  Confirmation rejects the whole request when the application is no
longer pending, the revision is stale, an active restriction window is hit
or another effective emergency license interferes.  The dedicated release
revokes the emergency license first, re-adjudicates the still-forced
licenses in suspension order (restoring passers, keeping blocked ones), and
finally drains the waitlist - all atomically.  Forced licenses may be
revoked ordinarily but cannot be resumed ordinarily; emergency licenses can
neither be suspended nor revoked ordinarily; stale and duplicate releases
change nothing; history snapshots and comparisons restore every stage.

Guard band is the test default (2.0); every application here shares the
default coverage circle and UTC window, so only frequency bands decide
interference.
"""
from factories import make_payload

# Default application window is 2026-10-01 08:00-10:00; restriction windows
# must end in the future, so tests that activate one use a 2030 day and put
# the affected applications on the same day.
FUTURE_START = "2030-10-01T08:00:00Z"
FUTURE_END = "2030-10-01T10:00:00Z"


def submit(client, low, high, name="Event", **overrides):
    payload = make_payload(freq=(low, high), name=name, **overrides)
    r = client.post("/api/applications", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit_future(client, low, high, name="Event"):
    return submit(
        client,
        low,
        high,
        name=name,
        start=FUTURE_START,
        end=FUTURE_END,
    )


def approve(client, app_id, expected):
    r = client.post(
        f"/api/applications/{app_id}/approve",
        json={"expected_revision": expected},
    )
    assert r.status_code == 200, r.text
    return r.json()


def revoke(client, app_id, reason="gone"):
    return client.post(
        f"/api/applications/{app_id}/revoke", json={"reason": reason}
    )


def register(client, app_id, reason=""):
    return client.post(
        f"/api/applications/{app_id}/waitlist", json={"reason": reason}
    )


def queue_ids(client):
    return client.get("/api/waitlist").json()["waitlist_application_ids"]


def rev(client):
    return client.get("/api/revision/current").json()["current_revision"]


def occupy_preview(client, app_id, reason="burst"):
    return client.post(
        "/api/emergency-occupations/preview",
        json={"application_id": app_id, "reason": reason},
    )


def occupy(client, app_id, expected, reason="burst"):
    return client.post(
        "/api/emergency-occupations",
        json={
            "application_id": app_id,
            "expected_revision": expected,
            "reason": reason,
        },
    )


def release(client, app_id, expected, reason="all clear"):
    return client.post(
        f"/api/emergency-occupations/{app_id}/release",
        json={"expected_revision": expected, "reason": reason},
    )


def window(client, low, high, expected, *, start=FUTURE_START,
           end=FUTURE_END, reason="control"):
    return client.post(
        "/api/restriction-windows",
        json={
            "circle": {"cx": 0, "cy": 0, "radius": 10},
            "frequency": {"low": low, "high": high},
            "time_window": {"start": start, "end": end},
            "reason": reason,
            "expected_revision": expected,
        },
    )


class TestPreview:
    def test_preview_is_read_only_and_lists_the_plan(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 200, 210, "ordinary B")  # far band, free
        approve(client, b, 1)  # revision 2
        e = submit(client, 105, 108, "emergency E")

        r = occupy_preview(client, e)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ready"] is True
        assert body["blockers"] == []
        assert body["current_revision"] == 2
        assert body["guard_band"] == 2.0
        # A [100,110] intersects the emergency band [105,108]; B [200,210]
        # does not.
        assert body["would_force_suspend_application_ids"] == [a]
        assert body["emergency_conflicts"] == []
        forced = body["would_force_suspend"][0]
        assert forced["application"]["id"] == a
        assert forced["is_emergency"] is False
        assert body["planned_suspension_start_revision"] == 3
        assert body["planned_approval_revision"] == 4
        assert body["emergency_waitlist_position"] is None

        # Nothing was mutated.
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{e}").json()["status"] == "pending"
        assert rev(client) == 2

    def test_preview_with_free_band_plans_one_revision(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)
        e = submit(client, 300, 310, "free emergency E")

        body = occupy_preview(client, e).json()
        assert body["ready"] is True
        assert body["would_force_suspend_application_ids"] == []
        assert body["planned_suspension_start_revision"] == 2
        assert body["planned_approval_revision"] == 2

    def test_preview_reports_status_and_window_blockers(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)

        # Missing application -> blocker, still 200.
        body = occupy_preview(client, 999999).json()
        assert body["ready"] is False
        assert [b["code"] for b in body["blockers"]] == ["not_found"]

        # Non-pending application -> blocker.
        body = occupy_preview(client, a).json()
        assert body["ready"] is False
        assert [b["code"] for b in body["blockers"]] == [
            "emergency_application_not_pending"
        ]

        # Window hit -> blocker alongside the forced suspension.
        fa = submit_future(client, 100, 110, "future A")
        approve(client, fa, 1)  # rev 2
        e = submit_future(client, 105, 108, "emergency E")
        wr = window(client, 105, 108, 2)
        assert wr.status_code == 201, wr.text  # rev 3
        body = occupy_preview(client, e).json()
        assert body["ready"] is False
        codes = [b["code"] for b in body["blockers"]]
        assert "restriction_window" in codes
        assert body["has_restriction_window_conflicts"] is True
        # Still read-only.
        assert client.get(f"/api/applications/{fa}").json()["status"] == "approved"
        assert rev(client) == 3

    def test_preview_flags_other_emergency_licenses_as_blockers(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)
        first = submit(client, 200, 210, "first emergency")
        r = occupy(client, first, 1)
        assert r.status_code == 201, r.text
        assert r.json()["approval_revision"] == 2

        second = submit(client, 205, 208, "second emergency")
        body = occupy_preview(client, second).json()
        assert body["ready"] is False
        codes = [b["code"] for b in body["blockers"]]
        assert "emergency_interference" in codes
        assert [x["application"]["id"] for x in body["emergency_conflicts"]] == [
            first
        ]
        assert body["emergency_conflicts"][0]["is_emergency"] is True
        # The first emergency is not proposed for forced suspension.
        assert body["would_force_suspend_application_ids"] == []


class TestConfirm:
    def test_confirm_forces_suspensions_then_approves_consecutively(self, client):
        a = submit(client, 100, 110, "A")
        b = submit(client, 200, 210, "B")
        approve(client, a, 0)  # rev 1
        approve(client, b, 1)  # rev 2
        e = submit(client, 105, 108, "emergency")

        r = occupy(client, e, 2)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["occupied"] is True
        # Forced suspension of A at rev 3, emergency approval at rev 4.
        assert body["suspension_start_revision"] == 3
        assert body["approval_revision"] == 4
        assert body["revision"] == 4
        assert body["final_revision"] == 4
        assert body["forced_suspension_application_ids"] == [a]
        forced = body["forced_suspensions"][0]
        assert forced["revision"] == 3
        assert forced["sequence"] == 0
        assert body["emergency_was_waitlisted"] is False

        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 4
        assert current["effective_application_ids"] == [b, e]
        assert current["suspended_application_ids"] == [a]
        assert current["emergency_suspended_application_ids"] == [a]
        assert current["active_emergency_application_ids"] == [e]

        # The forced suspension did NOT trigger a waitlist re-adjudication.
        assert "auto_approved_application_ids" not in body

        # A's original approval record is untouched; its forced marker links
        # the emergency.
        snapshot = client.get("/api/revision/4").json()
        a_record = next(
            x for x in snapshot["approvals"] if x["application_id"] == a
        )
        assert a_record["status_as_of"] == "suspended"
        assert a_record["suspended_at_revision"] == 3
        assert a_record["suspended_kind"] == "emergency"
        assert a_record["suspended_by_emergency_application_id"] == e
        e_record = next(
            x for x in snapshot["approvals"] if x["application_id"] == e
        )
        assert e_record["status_as_of"] == "approved"
        assert e_record["emergency"] is True
        assert snapshot["emergency_suspensions"] == [
            {
                "revision": 3,
                "application_id": a,
                "emergency_application_id": e,
                "emergency_approval_revision": 4,
                "sequence": 0,
                "reason": "burst",
                "created_at": snapshot["emergency_suspensions"][0]["created_at"],
            }
        ]
        assert snapshot["active_emergency_application_ids"] == [e]

    def test_confirm_with_free_band_consumes_one_revision(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 300, 310, "free emergency")

        body = occupy(client, e, 1).json()
        assert body["forced_suspension_count"] == 0
        assert body["suspension_start_revision"] == 2
        assert body["approval_revision"] == 2
        assert rev(client) == 2
        current = client.get("/api/revision/current").json()
        assert current["effective_application_ids"] == [a, e]
        assert current["emergency_suspended_application_ids"] == []

    def test_confirm_pulls_the_emergency_out_of_the_waitlist(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 105, 108, "emergency queued")
        register(client, e, "waiting")
        assert queue_ids(client) == [e]

        body = occupy(client, e, 1).json()
        assert body["emergency_was_waitlisted"] is True
        assert queue_ids(client) == []

    def test_forced_suspensions_are_ordered_by_application_id(self, client):
        # The two ordinary licenses clash with the emergency (whose UTC
        # window spans both) but are disjoint from each other in time, so
        # both could be approved beforehand.  The free license uses yet
        # another window so its id can sit between the forced ids.
        first = submit(
            client,
            100,
            110,
            "first",
            start="2026-10-01T08:00:00Z",
            end="2026-10-01T09:00:00Z",
        )
        free = submit(
            client,
            800,
            810,
            "free",
            start="2026-10-01T18:00:00Z",
            end="2026-10-01T19:00:00Z",
        )
        second = submit(
            client,
            102,
            106,
            "second",
            start="2026-10-01T11:00:00Z",
            end="2026-10-01T12:00:00Z",
        )
        approve(client, first, 0)
        approve(client, free, 1)
        approve(client, second, 2)
        e = submit(
            client,
            104,
            109,
            "emergency",
            start="2026-10-01T07:00:00Z",
            end="2026-10-01T13:00:00Z",
        )

        body = occupy(client, e, 3).json()
        assert body["forced_suspension_application_ids"] == [first, second]
        assert [x["revision"] for x in body["forced_suspensions"]] == [4, 5]
        assert body["approval_revision"] == 6
        assert rev(client) == 6

    def test_confirm_rejects_stale_revision_without_change(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 105, 108, "emergency")

        r = occupy(client, e, 0)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "stale_revision"
        assert rev(client) == 1
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{e}").json()["status"] == "pending"

    def test_confirm_rejects_non_pending_application(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        # Occupying an already approved application.
        r = occupy(client, a, 1)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "emergency_application_not_pending"
        assert rev(client) == 1

        r = client.post(
            "/api/emergency-occupations",
            json={"application_id": 999999, "expected_revision": 1},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_confirm_rejects_window_hit_as_a_whole(self, client):
        a = submit_future(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit_future(client, 105, 108, "emergency")
        wr = window(client, 106, 107, 1)
        assert wr.status_code == 201

        r = occupy(client, e, 2)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "restriction_window"
        assert len(err["details"]["restriction_window_conflicts"]) == 1
        # Nothing changed: A still effective, E still pending, revision 2.
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{e}").json()["status"] == "pending"
        assert rev(client) == 2

    def test_confirm_rejects_interference_with_another_emergency(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)
        first = submit(client, 200, 210, "emergency 1")
        occupy(client, first, 1)  # rev 2 (no forced suspensions)
        second = submit(client, 205, 208, "emergency 2")

        r = occupy(client, second, 2)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "emergency_interference"
        assert [c["application"]["id"] for c in err["details"]["conflicts"]] == [
            first
        ]
        # The first emergency stays effective; nothing is paused or consumed.
        assert rev(client) == 2
        assert client.get(f"/api/applications/{first}").json()["status"] == "approved"

    def test_two_non_interfering_emergencies_coexist(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)
        first = submit(client, 200, 210, "emergency 1")
        occupy(client, first, 1)  # rev 2
        second = submit(client, 300, 310, "emergency 2")
        body = occupy(client, second, 2).json()  # rev 3
        assert body["approval_revision"] == 3
        current = client.get("/api/revision/current").json()
        assert current["active_emergency_application_ids"] == [first, second]
        assert current["effective_application_ids"] == [a, first, second]


class TestOrdinaryOperationsGuards:
    def test_forced_license_cannot_be_resumed_ordinarily(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 105, 108, "emergency")
        occupy(client, e, 1)  # A forced-suspended rev 2, E approved rev 3

        r = client.post(
            f"/api/applications/{a}/resume",
            json={"expected_revision": 3, "reason": "try"},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "emergency_occupation_active"
        assert err["details"]["emergency_application_id"] == e
        # A stays suspended; no revision consumed.
        assert rev(client) == 3
        assert client.get(f"/api/applications/{a}").json()["status"] == "suspended"

    def test_forced_license_can_still_be_revoked_ordinarily(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 105, 108, "emergency")
        occupy(client, e, 1)  # rev 2, 3
        r = revoke(client, a, "obsolete while forced")
        assert r.status_code == 200, r.text
        assert r.json()["revoked"] is True
        assert client.get(f"/api/applications/{a}").json()["status"] == "revoked"
        # The revoked forced license is absent from the still-active
        # occupation's suspended set.
        current = client.get("/api/revision/current").json()
        assert a not in current["suspended_application_ids"]
        assert current["active_emergency_application_ids"] == [e]

    def test_revoking_forced_license_mid_occupation_drains_waitlist_normally(
        self, client
    ):
        # Revoking a forcibly suspended license uses the ordinary revoke path,
        # which DOES drain the waitlist; the emergency release later simply
        # skips the revoked license.
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 105, 108, "emergency")
        s = submit(client, 155, 158, "standby S")
        register(client, s)
        occupy(client, e, 1)  # A forced rev2, E approved rev3; no drain
        assert queue_ids(client) == [s]

        # Revoke the forced license: ordinary revoke drains the queue, S is
        # auto-approved at rev 5 (revoke rev4), release_revision=4.
        body = revoke(client, a, "obsolete").json()
        assert body["revision"] == 4
        assert body["auto_approved_application_ids"] == [s]
        assert body["auto_approvals"][0]["release_revision"] == 4

        # Release the occupation: no forced license remains to restore.
        released = release(client, e, 5).json()
        assert released["revocation_revision"] == 6
        assert released["restored_application_ids"] == []
        assert released["still_suspended_application_ids"] == []
        assert released["final_revision"] == 6
        current = client.get("/api/revision/current").json()
        assert current["effective_application_ids"] == [s]

    def test_emergency_license_cannot_be_suspended_or_revoked(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 105, 108, "emergency")
        occupy(client, e, 1)  # rev 2, 3

        s = client.post(
            f"/api/applications/{e}/suspend",
            json={"expected_revision": 3, "reason": "outage"},
        )
        assert s.status_code == 409
        assert s.json()["error"]["code"] == "emergency_occupation_active"

        r = revoke(client, e)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "emergency_occupation_active"
        assert rev(client) == 3
        assert client.get(f"/api/applications/{e}").json()["status"] == "approved"

    def test_ordinary_approve_against_active_emergency_is_interference(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 200, 210, "emergency")
        occupy(client, e, 1)  # rev 2
        other = submit(client, 205, 208, "ordinary overlap")
        r = client.post(
            f"/api/applications/{other}/approve",
            json={"expected_revision": 2},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "interference"
        assert rev(client) == 2

    def test_handover_cannot_target_an_emergency_license(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 200, 210, "emergency")
        occupy(client, e, 1)  # rev 2
        succ = submit(client, 250, 260, "successor")
        r = client.post(
            "/api/license-transfers",
            json={
                "source_application_id": e,
                "successor_application_id": succ,
                "expected_revision": 2,
                "reason": "swap",
            },
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "source_not_effective"


class TestRelease:
    def test_release_revokes_restores_and_drains_atomically(self, client):
        # A and B both clash with the emergency (spanning both morning and
        # afternoon) but are disjoint from each other in time, so both are
        # restored on release.  Standby C passes only once the emergency is
        # revoked.
        a = submit(
            client,
            100,
            110,
            "A",
            start="2026-10-01T08:00:00Z",
            end="2026-10-01T09:00:00Z",
        )
        b = submit(
            client,
            101,
            109,
            "B",
            start="2026-10-01T11:00:00Z",
            end="2026-10-01T12:00:00Z",
        )
        approve(client, a, 0)  # rev 1
        approve(client, b, 1)  # rev 2
        e = submit(
            client,
            105,
            108,
            "emergency",
            start="2026-10-01T07:00:00Z",
            end="2026-10-01T13:00:00Z",
        )
        c = submit(
            client,
            155,
            158,
            "standby C",
            start="2026-10-01T07:00:00Z",
            end="2026-10-01T13:00:00Z",
        )
        register(client, c, "waiting")
        occupy(client, e, 2)
        # forced A rev 3, forced B rev 4, E approved rev 5; queue untouched.
        assert queue_ids(client) == [c]

        body = release(client, e, 5).json()
        assert body["released"] is True
        # Emergency revocation rev 6; A restored rev 7, B restored rev 8;
        # standby C then auto-approved rev 9.
        assert body["revocation_revision"] == 6
        assert body["restored_application_ids"] == [a, b]
        assert [item["revision"] for item in body["restorations"]] == [7, 8]
        assert body["final_revision"] == 9
        assert rev(client) == 9
        # The waitlist was drained last.
        assert body["auto_approved_application_ids"] == [c]
        assert [
            item["release_revision"] for item in body["auto_approvals"]
        ] == [6]
        current = client.get("/api/revision/current").json()
        assert current["effective_application_ids"] == [a, b, c]

    def test_release_restores_in_suspension_order_blocked_stays(self, client):
        # A [100,110] and B [150,160] are both forced by E [100,160]; they
        # are disjoint from each other, so both restore.
        a = submit(client, 100, 110, "A")
        b = submit(client, 150, 160, "B")
        approve(client, a, 0)
        approve(client, b, 1)
        e = submit(client, 100, 160, "emergency")
        occupy(client, e, 2)  # A rev3, B rev4, E rev5

        body = release(client, e, 5).json()
        assert body["revocation_revision"] == 6
        assert body["restored_application_ids"] == [a, b]
        assert [item["revision"] for item in body["restorations"]] == [7, 8]
        assert body["still_suspended_application_ids"] == []
        assert body["revision"] == 8
        current = client.get("/api/revision/current").json()
        assert current["effective_application_ids"] == [a, b]
        assert current["suspended_application_ids"] == []
        assert current["active_emergency_application_ids"] == []
        assert client.get(f"/api/applications/{e}").json()["status"] == "revoked"

    def test_release_blocked_license_stays_suspended_with_reason(self, client):
        # A (morning) and B (afternoon) are disjoint in time so both could be
        # approved ordinarily, but both clash with the spanning emergency E1.
        # While E1 is active a second, non-interfering emergency E2 takes a
        # band that only B also covers; on E1's release A restores but B is
        # blocked by E2 and stays suspended.
        a = submit(
            client,
            100,
            104,
            "A",
            start="2026-10-01T08:00:00Z",
            end="2026-10-01T09:00:00Z",
        )
        b = submit(
            client,
            102,
            140,
            "B",
            start="2026-10-01T11:00:00Z",
            end="2026-10-01T12:00:00Z",
        )
        approve(client, a, 0)  # rev1
        approve(client, b, 1)  # rev2
        e1 = submit(
            client,
            105,
            108,
            "emergency 1",
            start="2026-10-01T07:00:00Z",
            end="2026-10-01T13:00:00Z",
        )
        occupy(client, e1, 2)  # A rev3, B rev4, E1 rev5

        # E2 only overlaps B's [102,140], not E1's [105,108] (22 MHz gap).
        e2 = submit(
            client,
            130,
            140,
            "emergency 2",
            start="2026-10-01T07:00:00Z",
            end="2026-10-01T13:00:00Z",
        )
        r2 = occupy(client, e2, 5)
        assert r2.status_code == 201, r2.text  # E2 rev6

        body = release(client, e1, 6).json()
        assert body["revocation_revision"] == 7
        assert body["restored_application_ids"] == [a]
        assert body["restorations"][0]["revision"] == 8
        assert body["still_suspended_application_ids"] == [b]
        blocked = body["still_suspended"][0]
        assert blocked["application_id"] == b
        assert blocked["sequence"] == 1
        assert blocked["conflicts"]
        assert blocked["conflicts"][0]["application"]["id"] == e2
        assert "interference" in blocked["reason"]
        assert body["final_revision"] == 8

        current = client.get("/api/revision/current").json()
        assert current["effective_application_ids"] == [a, e2]
        assert current["suspended_application_ids"] == [b]
        # E1 is over, so B is no longer an *emergency*-suspended license;
        # E2 still in force is the only active emergency.
        assert current["emergency_suspended_application_ids"] == []
        assert current["active_emergency_application_ids"] == [e2]

        # Ordinary resume still fails while E2 blocks B; then E2 is lifted,
        # and (A being time-disjoint) B resumes ordinarily.
        blocked_resume = client.post(
            f"/api/applications/{b}/resume",
            json={"expected_revision": 8, "reason": "try"},
        )
        assert blocked_resume.status_code == 409
        assert blocked_resume.json()["error"]["code"] == "interference"

        rel2 = release(client, e2, 8)
        assert rel2.status_code == 200, rel2.text
        # E2 had no forced licenses: just its revocation rev 9.
        assert rel2.json()["revocation_revision"] == 9
        ok = client.post(
            f"/api/applications/{b}/resume",
            json={"expected_revision": 9, "reason": "now free"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["revision"] == 10

    def test_release_skips_licenses_revoked_while_forced(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 105, 108, "emergency")
        occupy(client, e, 1)  # A rev2, E rev3
        revoke(client, a, "obsolete")  # rev4, drains waitlist
        body = release(client, e, 4).json()
        assert body["revocation_revision"] == 5
        assert body["restored_application_ids"] == []
        assert body["still_suspended_application_ids"] == []
        assert client.get(f"/api/applications/{a}").json()["status"] == "revoked"

    def test_release_window_blocked_license_stays_suspended(self, client):
        a = submit_future(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit_future(client, 105, 108, "emergency")
        occupy(client, e, 1)  # A rev2, E rev3
        # Activate a window hitting A while the occupation is active.
        wr = window(client, 100, 110, 3)
        assert wr.status_code == 201, wr.text  # rev4
        body = release(client, e, 4).json()
        assert body["restored_application_ids"] == []
        blocked = body["still_suspended"][0]
        assert blocked["application_id"] == a
        assert blocked["restriction_window_conflicts"]
        assert "window" in blocked["reason"]
        # The blocked license stays suspended even though the occupation ended.
        current = client.get("/api/revision/current").json()
        assert a in current["suspended_application_ids"]
        assert current["emergency_suspended_application_ids"] == []

    def test_release_uses_the_current_guard_band_for_readjudication(self, client):
        # Gap between the emergency band and A is exactly 1.0; under the
        # initial band 2.0 A is forced.  Tighten the band to 1.0 while the
        # occupation is active, so on release A restores even though the
        # emergency... the emergency is revoked first, and no other license
        # is near A, so this mainly asserts the resumption event carries the
        # live band.
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 111, 120, "emergency")  # 1 MHz gap < 2.0
        occupy(client, e, 1)  # A rev2, E rev3
        gb = client.post(
            "/api/config/guard-band",
            json={"guard_band": 1.0, "expected_revision": 3},
        )
        assert gb.status_code == 200, gb.text  # rev4
        body = release(client, e, 4).json()
        assert body["restorations"][0]["guard_band"] == 1.0
        snapshot = client.get("/api/revision/current").json()
        resumption = snapshot["emergency_resumptions"][0]
        assert resumption["guard_band"] == 1.0

    def test_stale_and_duplicate_release_change_nothing(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 105, 108, "emergency")
        occupy(client, e, 1)  # A rev2, E rev3

        stale = release(client, e, 1)
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_revision"
        assert rev(client) == 3
        assert client.get(f"/api/applications/{e}").json()["status"] == "approved"

        # Real release.
        first = release(client, e, 3)
        assert first.status_code == 200, first.text
        final_rev = first.json()["final_revision"]

        # Duplicate release with the current revision: not active anymore.
        dup = release(client, e, final_rev)
        assert dup.status_code == 409
        assert dup.json()["error"]["code"] == "emergency_occupation_not_active"
        assert rev(client) == final_rev

        # Unknown / never-emergency application.
        miss = release(client, 999999, final_rev)
        assert miss.status_code == 404
        plain = submit(client, 500, 510, "plain")
        nope = release(client, plain, final_rev)
        assert nope.status_code == 409
        assert nope.json()["error"]["code"] == "emergency_occupation_not_active"

    def test_release_waitlist_drain_is_fifo_and_blockers_keep_places(self, client):
        # Emergency frees no ordinary license at release; standby X passes
        # after the emergency is revoked, standby Y clashes with the
        # just-approved X and keeps its place.
        e = submit(client, 100, 110, "emergency")
        approve_for_setup = None
        # Approve the emergency directly via the occupation flow with no
        # forced licenses.
        occupy(client, e, 0)  # E approved rev1
        x = submit(client, 105, 108, "standby X")
        y = submit(client, 107, 109, "standby Y")
        register(client, x)
        register(client, y)
        body = release(client, e, 1).json()
        assert body["revocation_revision"] == 2
        assert body["auto_approved_application_ids"] == [x]
        assert body["remaining_waitlist_application_ids"] == [y]
        assert queue_ids(client) == [y]


class TestHistory:
    def test_snapshots_restore_every_stage_revision_by_revision(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # rev1
        e = submit(client, 105, 108, "emergency")
        occupy(client, e, 1)  # A forced rev2, E approved rev3

        # At rev 2: A is already forcibly suspended (the whole occupation
        # commits atomically, so the marker exists), even though the
        # emergency license itself is not approved until rev 3.
        snap2 = client.get("/api/revision/2").json()
        assert snap2["effective_application_ids"] == []
        assert snap2["suspended_application_ids"] == [a]
        assert snap2["emergency_suspended_application_ids"] == [a]
        # ...but no emergency license is effective before rev 3.
        assert snap2["active_emergency_application_ids"] == []
        assert len(snap2["emergency_suspensions"]) == 1
        activating = snap2["emergency_occupations"][0]
        assert activating["status"] == "activating"
        assert activating["approval_revision"] == 3
        assert activating["forced_suspension_application_ids"] == [a]
        assert snap2["active_emergency_occupations"] == []

        # At rev 3: the occupation is active; A is emergency-suspended.
        snap3 = client.get("/api/revision/3").json()
        assert snap3["effective_application_ids"] == [e]
        assert snap3["emergency_suspended_application_ids"] == [a]
        assert snap3["active_emergency_application_ids"] == [e]
        occupation = snap3["emergency_occupations"][0]
        assert occupation["application_id"] == e
        assert occupation["status"] == "active"
        assert occupation["suspension_start_revision"] == 2
        assert occupation["forced_suspension_application_ids"] == [a]
        assert occupation["restored_application_ids"] == []
        assert occupation["released_revision"] is None

        release(client, e, 3)  # E revoked rev4, A restored rev5

        snap4 = client.get("/api/revision/4").json()
        assert snap4["active_emergency_application_ids"] == []
        assert snap4["emergency_suspended_application_ids"] == [a]
        snap5 = client.get("/api/revision/5").json()
        assert snap5["effective_application_ids"] == [a]
        assert snap5["suspended_application_ids"] == []
        assert snap5["emergency_suspended_application_ids"] == []
        occupation = snap5["emergency_occupations"][0]
        assert occupation["status"] == "released"
        assert occupation["revocation_revision"] == 4
        assert occupation["released_revision"] == 5
        assert occupation["restored_application_ids"] == [a]
        assert snap5["emergency_resumptions"][0]["application_id"] == a
        assert snap5["emergency_resumptions"][0]["revision"] == 5

    def test_comparison_restores_emergency_stages(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # rev1
        e = submit(client, 105, 108, "emergency")
        occupy(client, e, 1)  # rev2, rev3
        release(client, e, 3)  # rev4, rev5

        cmp = client.get(
            "/api/revision/compare",
            params={"base_revision": 1, "target_revision": 5},
        ).json()
        types = [(ev["type"], ev["revision"]) for ev in cmp["events"]]
        assert ("emergency_suspension", 2) in types
        assert ("emergency_occupation_activated", 3) in types
        assert ("emergency_occupation_released", 5) in types
        assert ("emergency_resumption", 5) in types
        # The ordinary lifecycle events still drive status changes.
        status_changes = {
            (c["application_id"], c["to_status"]): c["revision"]
            for c in cmp["status_changes"]
        }
        assert status_changes[(a, "suspended")] == 2
        assert status_changes[(e, "approved")] == 3
        assert status_changes[(e, "revoked")] == 4
        assert status_changes[(a, "approved")] == 5
        # Both sides sit outside the occupation's lifetime (base rev 1 is
        # before activation, target rev 5 is after release): the sets are
        # empty on both sides while the interval events above carry the
        # activate/release stages.
        emergency_sets = cmp["active_emergency_application_ids"]
        assert emergency_sets["base"] == []
        assert emergency_sets["target"] == []
        assert emergency_sets["added"] == []
        assert emergency_sets["removed"] == []
        suspended_sets = cmp["emergency_suspended_application_ids"]
        assert suspended_sets["base"] == []
        assert suspended_sets["target"] == []
        assert suspended_sets["added"] == []
        assert suspended_sets["removed"] == []

        # A mid-occupation comparison shows the active emergency on one
        # side only.  The base revision (2) sits inside the forced-suspension
        # sequence, so the forced license is already emergency-suspended
        # there even though the emergency license is not effective yet.
        mid = client.get(
            "/api/revision/compare",
            params={"base_revision": 2, "target_revision": 3},
        ).json()
        assert mid["active_emergency_application_ids"]["added"] == [e]
        assert mid["active_emergency_application_ids"]["base"] == []
        assert mid["active_emergency_application_ids"]["target"] == [e]
        assert mid["emergency_suspended_application_ids"]["base"] == [a]
        assert mid["emergency_suspended_application_ids"]["target"] == [a]

        # Backward comparison keeps the labels.
        back = client.get(
            "/api/revision/compare",
            params={"base_revision": 5, "target_revision": 3},
        ).json()
        assert back["direction"] == "backward"
        assert back["base"]["revision"] == 5
        assert back["target"]["revision"] == 3

    def test_application_filter_on_comparison_matches_forced_license(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 105, 108, "emergency")
        occupy(client, e, 1)
        release(client, e, 3)
        cmp = client.get(
            "/api/revision/compare",
            params={
                "base_revision": 1,
                "target_revision": 5,
                "application_id": a,
            },
        ).json()
        types = {ev["type"] for ev in cmp["events"]}
        assert "emergency_suspension" in types
        assert "emergency_resumption" in types
        assert "emergency_occupation_activated" in types
        assert "emergency_occupation_released" in types


class TestListing:
    def test_list_and_get_occupations(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        e = submit(client, 105, 108, "emergency")
        occupy(client, e, 1, reason="burst traffic")
        active = client.get("/api/emergency-occupations").json()
        assert active["active_emergency_application_ids"] == [e]
        occ = active["active_occupations"][0]
        assert occ["status"] == "active"
        assert occ["forced_suspension_application_ids"] == [a]
        assert occ["reason"] == "burst traffic"
        assert active["released_occupations"] == []

        one = client.get(f"/api/emergency-occupations/{e}").json()["occupation"]
        assert one["application_id"] == e
        assert one["approval_revision"] == 3
        assert one["application"]["status"] == "approved"

        missing = client.get("/api/emergency-occupations/999999")
        assert missing.status_code == 404

        release(client, e, 3)
        listed = client.get(
            "/api/emergency-occupations", params={"include_inactive": "true"}
        ).json()
        assert listed["active_emergency_application_ids"] == []
        released = listed["released_occupations"][0]
        assert released["status"] == "released"
        assert released["revocation_revision"] == 4
        assert released["released_revision"] == 5
        assert released["forced_suspension_application_ids"] == [a]

        only_released = client.get(
            "/api/emergency-occupations", params={"status": "released"}
        ).json()
        assert len(only_released["released_occupations"]) == 1
        assert only_released["active_occupations"] == []

    def test_status_filter_validated(self, client):
        r = client.get("/api/emergency-occupations", params={"status": "bogus"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_failed"


class TestStructuralValidation:
    def test_bad_payload_shapes_are_422(self, client):
        r = client.post(
            "/api/emergency-occupations/preview", json={"application_id": 0}
        )
        assert r.status_code == 422
        r = client.post(
            "/api/emergency-occupations",
            json={"application_id": 1, "expected_revision": -1},
        )
        assert r.status_code == 422
        r = client.post(
            "/api/emergency-occupations/1/release", json={"expected_revision": -1}
        )
        assert r.status_code == 422
        # Revision untouched by rejected structural requests.
        assert rev(client) == 0
