"""Phase 2 gate — self-wake: timer + on-completion wake records and the tools."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from coworker.selfwake import Wake, WakeStore, selfwake_tools


def _now():
    return datetime.now(timezone.utc)


def test_timer_due_only_after_fire_time(tmp_path):
    store = WakeStore(tmp_path / "wakes.json")
    soon = store.add_timer("s1", _now() + timedelta(seconds=60))
    past = store.add_timer("s1", _now() - timedelta(seconds=1))
    due_ids = {w.id for w in store.due()}
    assert past.id in due_ids and soon.id not in due_ids


def test_completion_due_only_after_job_completes(tmp_path):
    store = WakeStore(tmp_path / "wakes.json")
    w = store.add_completion("s1", job_id="job-42")
    assert w.id not in {x.id for x in store.due()}  # not yet
    marked = store.complete_job("job-42")
    assert [x.id for x in marked] == [w.id]
    assert w.id in {x.id for x in store.due()}  # now due


def test_mark_fired_removes_from_due(tmp_path):
    store = WakeStore(tmp_path / "wakes.json")
    w = store.add_timer("s1", _now() - timedelta(seconds=1))
    store.mark_fired(w.id)
    assert w.id not in {x.id for x in store.due()}
    assert w.id not in {x.id for x in store.pending("s1")}


def test_persistence(tmp_path):
    store = WakeStore(tmp_path / "wakes.json")
    w = store.add_completion("s1", "job-1")
    reloaded = WakeStore(tmp_path / "wakes.json")
    assert any(x.id == w.id for x in reloaded.pending("s1"))


def test_event_due_only_after_event_fires(tmp_path):
    store = WakeStore(tmp_path / "wakes.json")
    w = store.add_event("s1", event_key="pr-opened")
    assert w.id not in {x.id for x in store.due()}
    marked = store.fire_event("pr-opened")
    assert [x.id for x in marked] == [w.id]
    assert w.id in {x.id for x in store.due()}


def test_selfwake_tools(tmp_path):
    store = WakeStore(tmp_path / "wakes.json")
    sleep_for, sleep_until, wake_on, wake_on_event = selfwake_tools(store, "s1")

    assert sleep_for(30)["ok"]
    assert wake_on("job-9")["job_id"] == "job-9"
    assert sleep_until((_now() + timedelta(minutes=5)).isoformat())["fire_at"]
    assert wake_on_event("alert-fired")["event_key"] == "alert-fired"

    pend = store.pending("s1")
    assert len(pend) == 4
    assert {w.kind for w in pend} == {"timer", "completion", "event"}


def test_wake_auto_pause_and_cancel_persist(tmp_path):
    path = tmp_path / "wakes.json"
    store = WakeStore(path)
    wake = store.add_timer("s1", _now() - timedelta(seconds=1), note="poll")
    assert [w.id for w in store.due()] == [wake.id]

    store.set_auto("s1", False)
    assert store.due() == []
    reloaded = WakeStore(path)
    assert reloaded.auto_enabled("s1") is False
    assert reloaded.cancel(wake.id, "wrong-session") is False
    assert reloaded.cancel(wake.id, "s1") is True
    assert reloaded.pending("s1") == []


def test_timer_due_handles_naive_and_aware_datetimes(tmp_path):
    path = tmp_path / "wakes.json"
    store = WakeStore(path)

    base = datetime(2030, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Exact normalized fire_at / instant for naive datetime -> interpreted as UTC
    naive_dt = datetime(2030, 6, 15, 12, 0, 0)
    w_naive = store.add_timer("s1", naive_dt, note="naive")
    assert w_naive.fire_at == "2030-06-15T12:00:00+00:00"

    # 2. Exact normalized fire_at / instant for aware datetime -> converted to UTC
    tz_plus7 = timezone(timedelta(hours=7))
    aware_dt = datetime(2030, 6, 15, 19, 0, 0, tzinfo=tz_plus7)
    w_aware = store.add_timer("s1", aware_dt, note="aware")
    assert w_aware.fire_at == "2030-06-15T12:00:00+00:00"

    # 3. Naive ISO string parsed in due() -> interpreted as UTC
    w_naive_iso = Wake("w-naive-iso", "s1", "timer", fire_at="2030-06-15T12:00:00")
    store._wakes[w_naive_iso.id] = w_naive_iso

    # 4. Aware ISO string with offset converted to UTC
    w_aware_iso = Wake("w-aware-iso", "s1", "timer", fire_at="2030-06-15T19:00:00+07:00")
    store._wakes[w_aware_iso.id] = w_aware_iso

    # 5. Corrupt non-string and malformed fire_at values must not crash
    corrupt_values = [12345, [2030, 6, 15], {"time": "2030"}, None, "", "   ", "not-a-date"]
    for i, bad_val in enumerate(corrupt_values):
        bad_w = Wake(f"bad-{i}", "s1", "timer", fire_at=bad_val)  # type: ignore
        store._wakes[bad_w.id] = bad_w

    # 6. Due boundary checks: before, exact, and after base instant
    # Before boundary: now = 1 second before base (both aware and naive now)
    before_utc = base - timedelta(seconds=1)
    before_naive = datetime(2030, 6, 15, 11, 59, 59)
    assert store.due(now=before_utc) == []
    assert store.due(now=before_naive) == []

    # Exact boundary: now == base
    exact_utc_ids = {w.id for w in store.due(now=base)}
    expected_ids = {w_naive.id, w_aware.id, w_naive_iso.id, w_aware_iso.id}
    assert expected_ids <= exact_utc_ids
    assert not any(w.id.startswith("bad-") for w in store.due(now=base))

    # Calling due() with naive now at boundary
    exact_naive_ids = {w.id for w in store.due(now=datetime(2030, 6, 15, 12, 0, 0))}
    assert expected_ids <= exact_naive_ids

    # After boundary: now = 1 second after base
    after_utc = base + timedelta(seconds=1)
    after_ids = {w.id for w in store.due(now=after_utc)}
    assert expected_ids <= after_ids

    # 7. sleep_until with naive ISO preserves UTC
    sleep_for, sleep_until, _, _ = selfwake_tools(store, "s1")
    su_naive = sleep_until("2030-06-15T12:00:00")
    assert su_naive["fire_at"] == "2030-06-15T12:00:00+00:00"
    assert store._wakes[su_naive["wake_id"]].fire_at == "2030-06-15T12:00:00+00:00"

    # sleep_until with aware ISO converts to UTC
    su_aware = sleep_until("2030-06-15T19:00:00+07:00")
    assert su_aware["fire_at"] == "2030-06-15T12:00:00+00:00"

    # Persistence verification: reload from disk
    store._save()
    reloaded = WakeStore(path)
    reloaded_ids = {w.id for w in reloaded.due(now=base)}
    assert expected_ids <= reloaded_ids
