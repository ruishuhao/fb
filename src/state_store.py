from __future__ import annotations

import json
from pathlib import Path


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = {
            "seen_ids": [],
            "snapshots": {},
            "active_ids": [],
            "missing_counts": {},
            "candidate_counts": {},
            "post_urls": {},
            "inactive_ids": [],
            "change_candidates": {},
            "notified_change_sigs": {},
            "event_last_sent": {},
            "extra_feed_top_phash": "",
            "extra_feed_top_ocr_norm": "",
            "extra_top_post_url": "",
            "extra_last_story_content_sig": "",
            "extra_last_story_clip_hash": "",
            "extra_pending_content_sig": "",
            "extra_pending_sig_count": 0,
            "extra_last_notified_ocr_time": "",
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = dict(self._state)
                for key, default_value in self._state.items():
                    value = data.get(key, default_value)
                    if isinstance(default_value, list) and not isinstance(value, list):
                        continue
                    if isinstance(default_value, dict) and not isinstance(value, dict):
                        continue
                    merged[key] = value
                self._state = merged
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @property
    def seen_ids(self) -> set[str]:
        return set(self._state.get("seen_ids", []))

    @property
    def snapshots(self) -> dict[str, str]:
        return dict(self._state.get("snapshots", {}))

    @property
    def active_ids(self) -> set[str]:
        return set(self._state.get("active_ids", []))

    @property
    def missing_counts(self) -> dict[str, int]:
        raw = self._state.get("missing_counts", {})
        if not isinstance(raw, dict):
            return {}
        output: dict[str, int] = {}
        for key, value in raw.items():
            try:
                output[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return output

    @property
    def candidate_counts(self) -> dict[str, int]:
        raw = self._state.get("candidate_counts", {})
        if not isinstance(raw, dict):
            return {}
        output: dict[str, int] = {}
        for key, value in raw.items():
            try:
                output[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return output

    @property
    def post_urls(self) -> dict[str, str]:
        raw = self._state.get("post_urls", {})
        if not isinstance(raw, dict):
            return {}
        output: dict[str, str] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str):
                output[key] = value
        return output

    @property
    def inactive_ids(self) -> set[str]:
        return set(self._state.get("inactive_ids", []))

    @property
    def change_candidates(self) -> dict[str, dict[str, int | str]]:
        raw = self._state.get("change_candidates", {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, int | str]] = {}
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            sig = v.get("sig")
            count = v.get("count")
            if isinstance(sig, str):
                try:
                    c = int(count)
                except (TypeError, ValueError):
                    c = 0
                out[k] = {"sig": sig, "count": c}
        return out

    @property
    def notified_change_sigs(self) -> dict[str, str]:
        raw = self._state.get("notified_change_sigs", {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, str):
                out[k] = v
        return out

    @property
    def event_last_sent(self) -> dict[str, int]:
        raw = self._state.get("event_last_sent", {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in raw.items():
            if not isinstance(k, str):
                continue
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    def mark_seen(self, post_ids: list[str], keep_latest: int = 500) -> None:
        current = self._state.get("seen_ids", [])
        merged = list(dict.fromkeys(post_ids + current))
        self._state["seen_ids"] = merged[:keep_latest]
        self.save()

    def upsert_snapshots(self, snapshot_map: dict[str, str], keep_latest: int = 1000) -> None:
        current = self._state.get("snapshots", {})
        if not isinstance(current, dict):
            current = {}
        for post_id, signature in snapshot_map.items():
            current[post_id] = signature

        if len(current) > keep_latest:
            keep_ids = self._state.get("seen_ids", [])[:keep_latest]
            trimmed = {k: current[k] for k in keep_ids if k in current}
            if len(trimmed) < keep_latest:
                for key, value in current.items():
                    if key not in trimmed:
                        trimmed[key] = value
                    if len(trimmed) >= keep_latest:
                        break
            current = trimmed

        self._state["snapshots"] = current
        self.save()

    def set_active_ids(self, post_ids: list[str], keep_latest: int = 1000) -> None:
        merged = list(dict.fromkeys(post_ids))
        self._state["active_ids"] = merged[:keep_latest]
        self.save()

    def set_missing_counts(self, counts: dict[str, int]) -> None:
        self._state["missing_counts"] = counts
        self.save()

    def set_candidate_counts(self, counts: dict[str, int]) -> None:
        self._state["candidate_counts"] = counts
        self.save()

    def upsert_post_urls(self, post_urls: dict[str, str], keep_latest: int = 1000) -> None:
        # Keep only current active-cycle URLs to avoid stale baseline confusion.
        current = dict(post_urls)
        if len(current) > keep_latest:
            trimmed: dict[str, str] = {}
            for key, value in current.items():
                trimmed[key] = value
                if len(trimmed) >= keep_latest:
                    break
            current = trimmed

        self._state["post_urls"] = current
        self.save()

    def set_inactive_ids(self, post_ids: list[str], keep_latest: int = 1000) -> None:
        merged = list(dict.fromkeys(post_ids))
        self._state["inactive_ids"] = merged[:keep_latest]
        self.save()

    def set_change_candidates(self, candidates: dict[str, dict[str, int | str]]) -> None:
        self._state["change_candidates"] = candidates
        self.save()

    def set_notified_change_sigs(self, change_sigs: dict[str, str]) -> None:
        self._state["notified_change_sigs"] = change_sigs
        self.save()

    def set_event_last_sent(self, event_last_sent: dict[str, int]) -> None:
        self._state["event_last_sent"] = event_last_sent
        self.save()

    def get_extra_feed_top_phash(self) -> str:
        v = self._state.get("extra_feed_top_phash", "")
        return v if isinstance(v, str) else ""

    def set_extra_feed_top_phash(self, value: str) -> None:
        self._state["extra_feed_top_phash"] = (value or "")[:128]
        self.save()

    def get_extra_feed_top_ocr_norm(self) -> str:
        v = self._state.get("extra_feed_top_ocr_norm", "")
        return v if isinstance(v, str) else ""

    def set_extra_feed_top_ocr_norm(self, value: str) -> None:
        self._state["extra_feed_top_ocr_norm"] = (value or "")[:2000]
        self.save()

    def get_extra_top_post_url(self) -> str:
        v = self._state.get("extra_top_post_url", "")
        return v if isinstance(v, str) else ""

    def set_extra_top_post_url(self, value: str) -> None:
        self._state["extra_top_post_url"] = (value or "")[:900]
        self.save()

    def get_extra_last_story_content_sig(self) -> str:
        v = self._state.get("extra_last_story_content_sig", "")
        return v if isinstance(v, str) else ""

    def set_extra_last_story_content_sig(self, value: str) -> None:
        self._state["extra_last_story_content_sig"] = (value or "")[:80]
        self.save()

    def get_extra_last_story_clip_hash(self) -> str:
        v = self._state.get("extra_last_story_clip_hash", "")
        return v if isinstance(v, str) else ""

    def set_extra_last_story_clip_hash(self, value: str) -> None:
        self._state["extra_last_story_clip_hash"] = (value or "")[:32]
        self.save()

    def get_extra_pending_content_sig(self) -> str:
        v = self._state.get("extra_pending_content_sig", "")
        return v if isinstance(v, str) else ""

    def get_extra_pending_sig_count(self) -> int:
        v = self._state.get("extra_pending_sig_count", 0)
        return v if isinstance(v, int) else 0

    def set_extra_pending_content_sig(self, sig: str, count: int) -> None:
        self._state["extra_pending_content_sig"] = (sig or "")[:80]
        self._state["extra_pending_sig_count"] = max(0, count)
        self.save()

    def get_extra_last_notified_ocr_time(self) -> str:
        return (self._state.get("extra_last_notified_ocr_time", "") or "").strip()

    def set_extra_last_notified_ocr_time(self, value: str) -> None:
        self._state["extra_last_notified_ocr_time"] = (value or "")[:120]
        self.save()
