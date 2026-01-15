import json
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _try_parse_legacy_created_at(created_at: Optional[str]) -> Optional[int]:
    if not created_at:
        return None
    try:
        dt = parsedate_to_datetime(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def unwrap_tweet_result(node: Any) -> Any:
    """
    Twitter GraphQL sometimes returns {"tweet": {...}} wrappers (e.g. limited actions / edits).
    Unwraps repeatedly when it looks like a wrapper.
    """
    cur = node
    for _ in range(3):
        if not isinstance(cur, dict):
            return cur
        inner = cur.get("tweet")
        if isinstance(inner, dict) and ("legacy" in inner or "edit_control" in inner or "core" in inner):
            cur = inner
            continue
        return cur
    return cur


def extract_entities(legacy: Dict[str, Any]) -> Dict[str, Any]:
    ent = legacy.get("entities") or {}
    hashtags = [h.get("text") for h in (ent.get("hashtags") or []) if isinstance(h, dict) and h.get("text")]
    symbols = [s.get("text") for s in (ent.get("symbols") or []) if isinstance(s, dict) and s.get("text")]
    mentions = [
        {
            "screen_name": m.get("screen_name"),
            "name": m.get("name"),
            "id_str": m.get("id_str"),
        }
        for m in (ent.get("user_mentions") or [])
        if isinstance(m, dict)
    ]
    urls = [
        {
            "url": u.get("url"),
            "expanded_url": u.get("expanded_url"),
            "display_url": u.get("display_url"),
        }
        for u in (ent.get("urls") or [])
        if isinstance(u, dict)
    ]
    return {
        "hashtags": hashtags,
        "symbols": symbols,
        "user_mentions": mentions,
        "urls": urls,
    }


def extract_media(legacy: Dict[str, Any]) -> List[Dict[str, Any]]:
    media_root = legacy.get("extended_entities") or legacy.get("entities") or {}
    media_lst = media_root.get("media") or []
    out: List[Dict[str, Any]] = []
    for m in media_lst:
        if not isinstance(m, dict):
            continue
        item: Dict[str, Any] = {
            "id_str": m.get("id_str"),
            "type": m.get("type"),
            "media_url_https": m.get("media_url_https"),
            "expanded_url": m.get("expanded_url"),
            "display_url": m.get("display_url"),
        }
        if isinstance(m.get("video_info"), dict):
            variants = m["video_info"].get("variants") or []
            item["video_variants"] = [
                {
                    "bitrate": v.get("bitrate"),
                    "content_type": v.get("content_type"),
                    "url": v.get("url"),
                }
                for v in variants
                if isinstance(v, dict)
            ]
        out.append(item)
    return out


def extract_text(tweet: Dict[str, Any]) -> Optional[str]:
    note = tweet.get("note_tweet")
    if isinstance(note, dict):
        results = note.get("note_tweet_results")
        if isinstance(results, dict):
            res = results.get("result")
            if isinstance(res, dict) and res.get("text"):
                return res.get("text")
    legacy = tweet.get("legacy") or {}
    return legacy.get("full_text") or legacy.get("text")


def extract_user(tweet: Dict[str, Any]) -> Dict[str, Any]:
    core = tweet.get("core") or {}
    ur = core.get("user_results") or {}
    res = ur.get("result") or {}
    legacy = res.get("legacy") or {}
    return {
        "id_str": legacy.get("id_str"),
        "rest_id": res.get("rest_id"),
        "name": legacy.get("name"),
        "screen_name": legacy.get("screen_name"),
        "verified": legacy.get("verified"),
        "followers_count": legacy.get("followers_count"),
        "friends_count": legacy.get("friends_count"),
        "statuses_count": legacy.get("statuses_count"),
    }


def extract_counts(tweet: Dict[str, Any]) -> Dict[str, Any]:
    legacy = tweet.get("legacy") or {}
    out = {
        "favorite_count": legacy.get("favorite_count"),
        "retweet_count": legacy.get("retweet_count"),
        "reply_count": legacy.get("reply_count"),
        "quote_count": legacy.get("quote_count"),
        "bookmark_count": legacy.get("bookmark_count"),
    }
    views = tweet.get("views")
    if isinstance(views, dict):
        out["view_count"] = views.get("count")
    return out


def extract_tweet_record(
    tweet_node: Any,
    *,
    url_fallback_screen_name: Optional[str] = None,
    editable_until_msecs: Optional[int] = None,
    context: Optional[Dict[str, Any]] = None,
    include_raw_legacy: bool = False,
) -> Optional[Dict[str, Any]]:
    tweet = unwrap_tweet_result(tweet_node)
    if not isinstance(tweet, dict):
        return None
    legacy = tweet.get("legacy")
    if not isinstance(legacy, dict):
        return None

    author = extract_user(tweet)
    screen_name = author.get("screen_name") or url_fallback_screen_name
    tweet_id = legacy.get("id_str") or tweet.get("rest_id")
    tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}" if screen_name and tweet_id else None

    created_ms = _try_parse_legacy_created_at(legacy.get("created_at"))
    if created_ms is None and editable_until_msecs is not None:
        created_ms = int(editable_until_msecs) - 3600000

    record: Dict[str, Any] = {
        "kind": "tweet",
        "tweet_id": tweet_id,
        "tweet_url": tweet_url,
        "created_at_ms": created_ms,
        "created_at_iso": _iso_from_ms(created_ms) if isinstance(created_ms, int) else None,
        "lang": legacy.get("lang"),
        "source": legacy.get("source"),
        "text": extract_text(tweet),
        "author": author,
        "counts": extract_counts(tweet),
        "conversation_id_str": legacy.get("conversation_id_str"),
        "in_reply_to_status_id_str": legacy.get("in_reply_to_status_id_str"),
        "in_reply_to_screen_name": legacy.get("in_reply_to_screen_name"),
        "quoted_status_id_str": legacy.get("quoted_status_id_str"),
        "is_quote_status": legacy.get("is_quote_status"),
        "possibly_sensitive": legacy.get("possibly_sensitive"),
        "entities": extract_entities(legacy),
        "media": extract_media(legacy),
    }

    if context:
        record["context"] = context

    if include_raw_legacy:
        record["raw_legacy"] = legacy

    return record


@dataclass
class JsonlWriter:
    path: Path

    def __post_init__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("w", encoding="utf-8", newline="\n")

    def write(self, obj: Dict[str, Any]) -> None:
        self._fp.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._fp.flush()

    def close(self) -> None:
        self._fp.close()

