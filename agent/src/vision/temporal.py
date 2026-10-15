"""TemporalReasoner — suy luận xuyên suốt chuỗi frames.

Module độc lập: nhận list frames đã describe từ VideoProcessor
dạng [{index, timestamp, text, path}], dùng logic thuần túy (stdlib)
để tìm thay đổi theo thời gian, tóm tắt diễn biến, phát hiện sự kiện.
"""

from __future__ import annotations

import re
from typing import Dict, List


def _tokenize(text: str) -> List[str]:
    """Tách từ đơn giản: lowercase, bỏ ký tự không phải chữ/số."""
    return re.findall(r"[^\W_]+", (text or "").lower())


class TemporalReasoner:
    """Suy luận theo thời gian trên chuỗi frames (pure logic, không dependencies)."""

    def __init__(self):
        pass  # pure logic, không cần dependencies

    # ------------------------------------------------------------------ #
    # track_changes
    # ------------------------------------------------------------------ #
    def track_changes(self, frames: List[Dict]) -> Dict:
        """So sánh text giữa các frames liên tiếp.

        Tìm từ/cụm xuất hiện mới (appeared) và từ/cụm biến mất (disappeared)
        bằng phép toán trên tập token (set difference).
        """
        if not frames:
            return {"timeline": [], "persistent": []}

        token_sets = [set(_tokenize(f.get("text", ""))) for f in frames]
        timeline: List[Dict] = []

        for i in range(1, len(frames)):
            prev, curr = token_sets[i - 1], token_sets[i]
            # Từ xuất hiện mới = có trong frame hiện tại nhưng không có trước đó
            appeared = sorted(curr - prev)
            # Từ biến mất = có trước đó nhưng frame hiện tại không còn
            disappeared = sorted(prev - curr)
            timeline.append(
                {
                    "from_frame": frames[i - 1].get("index", i - 1),
                    "to_frame": frames[i].get("index", i),
                    "appeared": appeared,
                    "disappeared": disappeared,
                }
            )

        # Text xuất hiện xuyên suốt = giao của mọi frame
        persistent: List[str] = []
        if token_sets:
            persistent = sorted(set.intersection(*token_sets))

        return {"timeline": timeline, "persistent": persistent}

    # ------------------------------------------------------------------ #
    # summarize
    # ------------------------------------------------------------------ #
    def summarize(self, frames: List[Dict]) -> Dict:
        """Tóm tắt diễn biến theo thời gian."""
        if not frames:
            return {
                "total_frames": 0,
                "duration": 0.0,
                "key_moments": [],
                "narrative": "Không có frame nào để tóm tắt.",
            }

        total_frames = len(frames)
        timestamps = [f.get("timestamp", 0) or 0 for f in frames]
        # duration = khoảng cách giữa timestamp đầu và cuối
        duration = float(timestamps[-1] - timestamps[0]) if len(timestamps) > 1 else 0.0
        if duration < 0:
            duration = 0.0

        # key_moments: các frame có thay đổi lớn so với frame trước
        changes = self.track_changes(frames)
        key_moments: List[Dict] = []
        for step in changes["timeline"]:
            change_count = len(step["appeared"]) + len(step["disappeared"])
            prev_idx = step["from_frame"]
            prev_len = len(set(_tokenize(frames[prev_idx].get("text", "")))) if prev_idx < len(frames) else 0
            # "Thay đổi lớn": từ mới/biến mất chiếm ít nhất 50% số token frame trước
            if prev_len and change_count / prev_len >= 0.5:
                key_moments.append(
                    {
                        "frame": step["to_frame"],
                        "timestamp": frames[step["to_frame"]].get("timestamp", 0),
                        "appeared": step["appeared"],
                        "disappeared": step["disappeared"],
                    }
                )

        persistent = changes["persistent"]
        narrative = (
            f"Chuỗi gồm {total_frames} frame trong {duration:.1f} giây. "
            f"Phát hiện {len(key_moments)} thời điểm thay đổi đáng kể."
        )
        if persistent:
            narrative += " Các yếu tố xuyên suốt: " + ", ".join(persistent[:5]) + "."
        if key_moments:
            moments_desc = "; ".join(
                f"frame {m['frame']} (xuất hiện: {', '.join(m['appeared']) or 'không có'})"
                for m in key_moments[:3]
            )
            narrative += " Diễn biến: " + moments_desc + "."

        return {
            "total_frames": total_frames,
            "duration": duration,
            "key_moments": key_moments,
            "narrative": narrative,
        }

    # ------------------------------------------------------------------ #
    # detect_events
    # ------------------------------------------------------------------ #
    def detect_events(self, frames: List[Dict], threshold: float = 0.5) -> List[Dict]:
        """Phát hiện "sự kiện": thay đổi đột ngột giữa 2 frames liên tiếp.

        Đo bằng Jaccard distance giữa 2 tập token:
            Jaccard(A, B) = 1 - |A ∩ B| / |A ∪ B|
        - A, B là tập token của 2 frames liên tiếp.
        - |A ∩ B|: số từ chung cả 2 frame.
        - |A ∪ B|: số từ khác nhau gộp lại.
        - Kết quả: 0.0 = 2 frame giống hệt nhau, 1.0 = hoàn toàn khác nhau.
        Nếu distance > threshold → coi là một sự kiện.
        """
        if not frames or len(frames) < 2:
            return []

        events: List[Dict] = []
        token_sets = [set(_tokenize(f.get("text", ""))) for f in frames]

        for i in range(1, len(frames)):
            a, b = token_sets[i - 1], token_sets[i]
            union = a | b
            if not union:
                # Cả 2 frame đều rỗng text → không thay đổi, distance = 0
                distance = 0.0
            else:
                intersection = a & b
                # Jaccard distance: 1 trừ Jaccard similarity
                distance = 1.0 - len(intersection) / len(union)

            if distance > threshold:
                appeared = sorted(b - a)
                disappeared = sorted(a - b)
                desc_parts = []
                if appeared:
                    desc_parts.append("xuất hiện: " + ", ".join(appeared))
                if disappeared:
                    desc_parts.append("biến mất: " + ", ".join(disappeared))
                events.append(
                    {
                        "at_frame": frames[i].get("index", i),
                        "timestamp": frames[i].get("timestamp", 0),
                        "description": "; ".join(desc_parts) or "thay đổi nội dung",
                        "change_ratio": round(distance, 3),
                    }
                )

        return events
