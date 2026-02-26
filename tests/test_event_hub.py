"""Unit tests for live event hub buffering and snapshots."""

from __future__ import annotations

from event_hub import EventHub


def test_event_hub_snapshot_filters_by_since_and_topics() -> None:
    hub = EventHub(buffer_size=10)
    hub.publish(event_type="request.completed", source="server", trace_id="a", payload={"x": 1})
    hub.publish(event_type="scenario.run.started", source="server", trace_id="b", payload={"x": 2})
    hub.publish(event_type="request.completed", source="server", trace_id="c", payload={"x": 3})

    snapshot = hub.snapshot(since_id=1, limit=10, topics={"request.completed"})

    assert snapshot["latest_id"] == 3
    assert [event["id"] for event in snapshot["events"]] == [3]
    assert snapshot["overflowed"] is False


def test_event_hub_reports_overflow_when_since_id_is_stale() -> None:
    hub = EventHub(buffer_size=3)
    for index in range(150):
        hub.publish(
            event_type="request.completed",
            source="server",
            trace_id=str(index),
            payload={"i": index},
        )

    snapshot = hub.snapshot(since_id=1, limit=10, topics=None)

    assert snapshot["overflowed"] is True
    assert snapshot["oldest_id"] > 1
    assert snapshot["latest_id"] == 150
