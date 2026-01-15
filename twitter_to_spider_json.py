import argparse
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _strip_jsonc_comments(text: str) -> str:
    out: List[str] = []
    i = 0
    n = len(text)
    in_string = False
    string_quote = '"'
    escape = False
    in_line_comment = False
    in_block_comment = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ''

        if in_line_comment:
            if ch == '\n':
                in_line_comment = False
                out.append(ch)
            i += 1
            continue

        if in_block_comment:
            if ch == '*' and nxt == '/':
                in_block_comment = False
                i += 2
                continue
            if ch == '\n':
                out.append(ch)
            i += 1
            continue

        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == string_quote:
                in_string = False
            i += 1
            continue

        if ch in ('"', "'"):
            in_string = True
            string_quote = ch
            out.append(ch)
            i += 1
            continue

        if ch == '/' and nxt == '/':
            in_line_comment = True
            i += 2
            continue

        if ch == '/' and nxt == '*':
            in_block_comment = True
            i += 2
            continue

        out.append(ch)
        i += 1

    return ''.join(out)


def load_settings(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        raw = f.read()
    return json.loads(_strip_jsonc_comments(raw))


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _format_msecs(ms: Any) -> str:
    try:
        ms_int = int(ms)
    except Exception:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms_int / 1000))


_STATUS_RE = re.compile(r"/status/(\d+)")


def _extract_tweet_id(tweet_url: str) -> str:
    m = _STATUS_RE.search(tweet_url or "")
    return m.group(1) if m else ""


def _normalize_username(raw: str) -> str:
    s = (raw or "").strip()
    s = s.lstrip("@")
    return s


def _iter_records(path: Path) -> Iterable[Dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec
        return

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item


def _find_latest_record_file(folder_path: Path) -> Optional[Path]:
    patterns = ["*-media.json", "*-media.jsonl", "*-text.json", "*-text.jsonl"]
    candidates: List[Path] = []
    for pat in patterns:
        candidates.extend(folder_path.glob(pat))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _read_file_b64(path: Path) -> Optional[str]:
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    return base64.b64encode(raw).decode("utf-8")


def build_notes(
    records: Iterable[Dict[str, Any]],
    *,
    include_image_base64: bool = False,
) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        tweet_url = _to_str(rec.get("tweet_url"))
        tweet_id = _extract_tweet_id(tweet_url) or tweet_url
        grouped.setdefault(tweet_id, []).append(rec)

    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    notes: List[Dict[str, Any]] = []

    for tweet_id, recs in grouped.items():
        base = recs[0]
        user_name = _normalize_username(_to_str(base.get("user_name")))
        tweet_url = _to_str(base.get("tweet_url"))
        canonical_url = f"https://x.com/{user_name}/status/{tweet_id}" if user_name and tweet_id.isdigit() else tweet_url

        tweet_content = _to_str(base.get("tweet_content"))
        title = tweet_content.strip().splitlines()[0] if tweet_content.strip() else ""
        if len(title) > 120:
            title = title[:117] + "..."

        image_list: List[str] = []
        image_base64: List[str] = []
        video_addr: Optional[str] = None

        for r in recs:
            media_type = _to_str(r.get("media_type"))
            media_url = _to_str(r.get("media_url"))
            saved_path = _to_str(r.get("saved_path"))
            if media_type.lower() == "image":
                if media_url:
                    image_list.append(media_url)
                if include_image_base64 and saved_path:
                    b64 = _read_file_b64(Path(saved_path))
                    if b64:
                        image_base64.append(b64)
            elif media_type.lower() == "video":
                if not video_addr and media_url:
                    video_addr = media_url

        note_type = "视频" if video_addr else ("图集" if image_list else "文本")

        note: Dict[str, Any] = {
            "note_id": tweet_id if tweet_id else "",
            "note_url": canonical_url,
            "note_type": note_type,
            "title": title,
            "desc": tweet_content,
            "tags": [],
            "upload_time": _format_msecs(base.get("tweet_date_ms")) or _to_str(base.get("tweet_date")),
            "user_id": user_name,
            "nickname": _to_str(base.get("display_name")),
            "avatar": "",
            "home_url": f"https://x.com/{user_name}" if user_name else "",
            "ip_location": "",
            "liked_count": _to_str(base.get("favorite_count")),
            "collected_count": "",
            "comment_count": _to_str(base.get("reply_count")),
            "share_count": _to_str(base.get("retweet_count")),
            "comments": [],
            "image_list": image_list,
            "video_addr": video_addr,
            "video_cover": None,
            "style_analysis": "未生成分析",
            "style_updated_at": now_str,
        }
        # Keep output schema aligned with Spider_XHS *_no_images.json by default.
        if include_image_base64:
            note["image_base64"] = image_base64
        notes.append(note)

    def _sort_key(n: Dict[str, Any]):
        return (n.get("upload_time") or "", n.get("note_id") or "")

    notes.sort(key=_sort_key, reverse=True)
    return notes


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert twitter_download search outputs to Spider_XHS-style JSON schema.")
    parser.add_argument("--settings", default="settings.json", help="Path to settings.json")
    parser.add_argument("--folder", required=True, help="Search output folder name (same as search_down --folder)")
    parser.add_argument("--output-dir", default="datas/json_datas", help="Output directory (default: datas/json_datas)")
    parser.add_argument("--output-name", required=True, help="Output JSON base name (without .json)")
    parser.add_argument(
        "--include-image-base64",
        action="store_true",
        help="Include note['image_base64'] (default: omitted to match *_no_images.json style)",
    )
    args = parser.parse_args()

    settings = load_settings(args.settings)
    save_path = settings.get("save_path") or os.path.join(os.getcwd(), "data")
    folder_path = Path(save_path) / args.folder
    if not folder_path.exists():
        raise SystemExit(f"Search folder not found: {folder_path}")

    record_file = _find_latest_record_file(folder_path)
    if record_file is None:
        raise SystemExit(f"No search record file found in {folder_path} (expected *-media.json/jsonl)")

    notes = build_notes(_iter_records(record_file), include_image_base64=args.include_image_base64)
    out_path = Path(args.output_dir) / f"{args.output_name}.json"
    write_json(out_path, notes)
    print(f"Wrote {len(notes)} notes to {out_path}")


if __name__ == "__main__":
    main()
