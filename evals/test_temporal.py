"""Test cho TemporalReasoner (dữ liệu giả, stdlib only)."""

from temporal import TemporalReasoner


def _mk(idx, ts, text):
    return {"index": idx, "timestamp": ts, "text": text, "path": f"frame_{idx}.png"}


def test_track_changes_basic():
    r = TemporalReasoner()
    frames = [_mk(0, 0.0, "hello world"), _mk(1, 1.0, "hello vietnam")]
    out = r.track_changes(frames)
    assert len(out["timeline"]) == 1
    step = out["timeline"][0]
    assert step["from_frame"] == 0
    assert step["to_frame"] == 1
    assert step["appeared"] == ["vietnam"], step
    assert step["disappeared"] == ["world"], step
    assert out["persistent"] == ["hello"], out
    print("PASS: track_changes basic")


def test_track_changes_no_change():
    r = TemporalReasoner()
    frames = [_mk(0, 0.0, "same text"), _mk(1, 1.0, "same text")]
    out = r.track_changes(frames)
    assert out["timeline"][0]["appeared"] == []
    assert out["timeline"][0]["disappeared"] == []
    assert out["persistent"] == ["same", "text"]
    print("PASS: track_changes no change")


def test_summarize():
    r = TemporalReasoner()
    frames = [
        _mk(0, 0.0, "xe hơi đỏ chạy trên đường"),
        _mk(1, 1.0, "xe hơi đỏ chạy trên đường"),
        _mk(2, 2.0, "bầu trời xanh mây trắng"),
        _mk(3, 3.0, "bầu trời xanh mây trắng mưa lớn"),
    ]
    out = r.summarize(frames)
    assert out["total_frames"] == 4
    assert out["duration"] == 3.0
    assert out["narrative"], "narrative không được rỗng"
    assert isinstance(out["narrative"], str)
    # frame 2 thay đổi hoàn toàn so với frame 1 → key moment
    key_frames = [m["frame"] for m in out["key_moments"]]
    assert 2 in key_frames, out["key_moments"]
    print("PASS: summarize (narrative + key_moments)")


def test_summarize_empty():
    r = TemporalReasoner()
    out = r.summarize([])
    assert out["total_frames"] == 0
    assert out["duration"] == 0.0
    assert out["key_moments"] == []
    assert out["narrative"], "narrative cho frames rỗng không được rỗng"
    print("PASS: summarize empty")


def test_detect_events():
    r = TemporalReasoner()
    frames = [
        _mk(0, 0.0, "xe hơi đỏ chạy trên đường"),
        _mk(1, 1.0, "xe hơi đỏ chạy trên đường nhựa"),
        _mk(2, 2.0, "bầu trời xanh mây trắng"),
        _mk(3, 3.0, "bầu trời xanh mây trắng"),
    ]
    events = r.detect_events(frames, threshold=0.5)
    # frame 2 thay đổi đột ngột so với frame 1 → event; frame 1 thay đổi nhỏ (thêm "nhựa") → không event
    at_frames = [e["at_frame"] for e in events]
    assert at_frames == [2], events
    ev = events[0]
    assert ev["timestamp"] == 2.0
    assert ev["change_ratio"] > 0.5
    assert ev["description"], "description không được rỗng"
    print("PASS: detect_events sudden change")


def test_detect_events_none():
    r = TemporalReasoner()
    frames = [_mk(0, 0.0, "a b c"), _mk(1, 1.0, "a b c")]
    assert r.detect_events(frames) == []
    print("PASS: detect_events no event")


def test_edge_empty():
    r = TemporalReasoner()
    assert r.track_changes([]) == {"timeline": [], "persistent": []}
    assert r.detect_events([]) == []
    assert r.detect_events([_mk(0, 0.0, "solo")]) == []
    print("PASS: edge cases (empty frames) không crash")


if __name__ == "__main__":
    test_track_changes_basic()
    test_track_changes_no_change()
    test_summarize()
    test_summarize_empty()
    test_detect_events()
    test_detect_events_none()
    test_edge_empty()
    print("ALL TESTS PASSED (7/7)")
