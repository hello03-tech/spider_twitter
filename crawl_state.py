import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union


STATE_FILENAME = ".crawl_state.json"


def build_run_key(*, time_range: str, has_retweet: bool, has_highlights: bool, has_likes: bool) -> str:
    payload = {
        "time_range": str(time_range or ""),
        "has_retweet": bool(has_retweet),
        "has_highlights": bool(has_highlights),
        "has_likes": bool(has_likes),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def state_path(save_path: Union[str, os.PathLike]) -> Path:
    return Path(save_path) / STATE_FILENAME


def load_state(save_path: Union[str, os.PathLike], *, run_key: str) -> Optional[Dict[str, Any]]:
    path = state_path(save_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("run_key") != run_key:
        return None
    return data


def save_state(
    save_path: Union[str, os.PathLike],
    *,
    run_key: str,
    cursor: Optional[str],
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {
        "version": 1,
        "run_key": run_key,
        "updated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "cursor": cursor,
        "incomplete": True,
    }
    if extra:
        payload.update(extra)  # type: ignore[arg-type]

    path = state_path(save_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_fd, tmp_name = tempfile.mkstemp(prefix=STATE_FILENAME + ".", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def clear_state(save_path: Union[str, os.PathLike]) -> None:
    path = state_path(save_path)
    try:
        path.unlink()
    except FileNotFoundError:
        return


_MEDIA_INDEX_RE = re.compile(r"-(?:img|vid)_(\d+)\.(?:jpg|jpeg|png|gif|mp4|webm)$", re.IGNORECASE)


def infer_existing_media_count(save_path: Union[str, os.PathLike]) -> int:
    """
    Infer the next counter value used in main.py file naming to avoid overwriting
    when resuming/rerunning in the same user directory.
    """
    root = Path(save_path)
    if not root.exists():
        return 0
    best = -1
    for p in root.iterdir():
        if not p.is_file():
            continue
        m = _MEDIA_INDEX_RE.search(p.name)
        if not m:
            continue
        try:
            best = max(best, int(m.group(1)))
        except Exception:
            continue
    return 0 if best < 0 else best + 1
