"""TemporalReasoner — reasoning across a sequence of frames.

Standalone module: takes a list of described frames from VideoProcessor of
the form [{index, timestamp, text, path}], and uses pure logic (stdlib) to
find changes over time, summarize the progression, and detect events.
"""

from __future__ import annotations

import re
from typing import Dict, List


def _tokenize(text: str) -> List[str]:
    """Simple tokenization: lowercase, drop non-letter/digit chars."""
    return re.findall(r"[^\W_]+", (text or "").lower())


class TemporalReasoner:
    """Time-based reasoning over a frame sequence (pure logic, no dependencies)."""

    def __init__(self):
        pass  # pure logic, no dependencies needed

    # ------------------------------------------------------------------ #
    # track_changes
    # ------------------------------------------------------------------ #
    def track_changes(self, frames: List[Dict]) -> Dict:
        """Compares text between consecutive frames.

        Finds newly appearing (appeared) and vanishing (disappeared) words
        via token-set differences.
        """
        if not frames:
            return {"timeline": [], "persistent": []}

        token_sets = [set(_tokenize(f.get("text", ""))) for f in frames]
        timeline: List[Dict] = []

        for i in range(1, len(frames)):
            prev, curr = token_sets[i - 1], token_sets[i]
            # Appeared words = in the current frame but not the previous
            appeared = sorted(curr - prev)
            # Disappeared words = in the previous frame but not the current
            disappeared = sorted(prev - curr)
            timeline.append(
                {
                    "from_frame": frames[i - 1].get("index", i - 1),
                    "to_frame": frames[i].get("index", i),
                    "appeared": appeared,
                    "disappeared": disappeared,
                }
            )

        # Persistent text = intersection of all frames
        persistent: List[str] = []
        if token_sets:
            persistent = sorted(set.intersection(*token_sets))

        return {"timeline": timeline, "persistent": persistent}

    # ------------------------------------------------------------------ #
    # summarize
    # ------------------------------------------------------------------ #
    def summarize(self, frames: List[Dict]) -> Dict:
        """Summarizes the progression over time."""
        if not frames:
            return {
                "total_frames": 0,
                "duration": 0.0,
                "key_moments": [],
                "narrative": "No frames to summarize.",
            }

        total_frames = len(frames)
        timestamps = [f.get("timestamp", 0) or 0 for f in frames]
        # duration = span between the first and last timestamps
        duration = float(timestamps[-1] - timestamps[0]) if len(timestamps) > 1 else 0.0
        if duration < 0:
            duration = 0.0

        # key_moments: frames that changed a lot vs the previous frame
        changes = self.track_changes(frames)
        key_moments: List[Dict] = []
        for step in changes["timeline"]:
            change_count = len(step["appeared"]) + len(step["disappeared"])
            prev_idx = step["from_frame"]
            prev_len = len(set(_tokenize(frames[prev_idx].get("text", "")))) if prev_idx < len(frames) else 0
            # "Big change": new/gone words make up at least 50% of the previous frame's tokens
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
            f"Sequence of {total_frames} frames over {duration:.1f} seconds. "
            f"Detected {len(key_moments)} significant change point(s)."
        )
        if persistent:
            narrative += " Persistent elements: " + ", ".join(persistent[:5]) + "."
        if key_moments:
            moments_desc = "; ".join(
                f"frame {m['frame']} (appeared: {', '.join(m['appeared']) or 'none'})"
                for m in key_moments[:3]
            )
            narrative += " Progression: " + moments_desc + "."

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
        """Detects "events": sudden changes between 2 consecutive frames.

        Measured with Jaccard distance between the 2 token sets:
            Jaccard(A, B) = 1 - |A ∩ B| / |A ∪ B|
        - A, B are the token sets of 2 consecutive frames.
        - |A ∩ B|: words shared by both frames.
        - |A ∪ B|: distinct words combined.
        - Result: 0.0 = the 2 frames are identical, 1.0 = totally different.
        When distance > threshold → counts as an event.
        """
        if not frames or len(frames) < 2:
            return []

        events: List[Dict] = []
        token_sets = [set(_tokenize(f.get("text", ""))) for f in frames]

        for i in range(1, len(frames)):
            a, b = token_sets[i - 1], token_sets[i]
            union = a | b
            if not union:
                # Both frames empty text → no change, distance = 0
                distance = 0.0
            else:
                intersection = a & b
                # Jaccard distance: 1 minus Jaccard similarity
                distance = 1.0 - len(intersection) / len(union)

            if distance > threshold:
                appeared = sorted(b - a)
                disappeared = sorted(a - b)
                desc_parts = []
                if appeared:
                    desc_parts.append("appeared: " + ", ".join(appeared))
                if disappeared:
                    desc_parts.append("disappeared: " + ", ".join(disappeared))
                events.append(
                    {
                        "at_frame": frames[i].get("index", i),
                        "timestamp": frames[i].get("timestamp", 0),
                        "description": "; ".join(desc_parts) or "content changed",
                        "change_ratio": round(distance, 3),
                    }
                )

        return events
