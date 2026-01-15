import argparse
import base64
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Tuple

import requests

try:
    from PIL import Image  # type: ignore
    import io

    _PIL_OK = True
except Exception:
    Image = None
    io = None
    _PIL_OK = False

BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://14.103.60.158:3001/")
API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL = os.environ.get("MODEL", "gemini-2.5-flash")


def _detect_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def shrink_image_b64(image_b64: str) -> Tuple[str, str]:
    raw = base64.b64decode(image_b64)
    if not _PIL_OK:
        return image_b64, _detect_mime(raw)

    with Image.open(io.BytesIO(raw)) as img:
        img = img.convert("RGB")
        img.thumbnail((800, 800))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
    return base64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"


def call_llm_for_image(image_b64: str, mime: str, idx: int) -> str:
    api_url = f"{BASE_URL.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    prompt_text = (
        "Extract every piece of visible text (Chinese or English) from the provided screenshot. "
        "List them in the order they appear."
    )
    message_content = [
        {"type": "text", "text": prompt_text},
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{image_b64}",
                "description": f"Image {idx}",
            },
        },
    ]
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a text extraction assistant."},
            {"role": "user", "content": message_content},
        ],
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    resp = requests.post(api_url, headers=headers, json=payload, timeout=90)
    resp.raise_for_status()
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message", {}) or {}).get("content", "").strip()


def write_results(path: Path, records: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def _normalize_note_id(note_id) -> Optional[str]:
    if note_id is None:
        return None
    if isinstance(note_id, (str, int)):
        s = str(note_id).strip()
        return s or None
    return None


def _normalize_image_list(image_list) -> list[str]:
    if not image_list:
        return []
    if isinstance(image_list, list):
        return [x for x in image_list if isinstance(x, str) and x.strip()]
    return []


def _build_images(image_urls: list[str], extracted: list[dict]) -> list[dict]:
    images: list[dict] = [{"index": i, "url": url, "text": ""} for i, url in enumerate(image_urls, 1)]
    for item in extracted or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except Exception:
            continue
        text = item.get("text", "") or ""
        if 1 <= idx <= len(images):
            images[idx - 1]["text"] = text
        else:
            images.append({"index": idx, "url": "", "text": text})
    return images


def _default_merged_path(input_path: Path) -> Path:
    if input_path.stem.endswith("_no_images"):
        return input_path
    return input_path.with_name(f"{input_path.stem}_no_images{input_path.suffix}")


def _upsert_extracted(existing: list[dict], record: dict) -> list[dict]:
    note_id = _normalize_note_id(record.get("note_id"))
    if not note_id:
        return existing
    out: list[dict] = []
    replaced = False
    for r in existing:
        if isinstance(r, dict) and _normalize_note_id(r.get("note_id")) == note_id:
            if not replaced:
                out.append({"note_id": note_id, "images": record.get("images") or []})
                replaced = True
            continue
        out.append(r)
    if not replaced:
        out.append({"note_id": note_id, "images": record.get("images") or []})
    return out


def process_image_task(idx: int, raw_b64: str) -> str:
    shrunk, mime = shrink_image_b64(raw_b64)
    return call_llm_for_image(shrunk, mime, idx)


def parse_args():
    parser = argparse.ArgumentParser(description="Extract image texts for Spider-style notes JSON.")
    parser.add_argument("--note-ids", "-n", nargs="+", help="Only process these note IDs (default: all)")
    parser.add_argument("--skip-images", action="store_true", help="Do not call the LLM for image text extraction")
    parser.add_argument("--output-name", "-o", default="extracted_texts", help="Base name for the output JSON file")
    parser.add_argument("--output-dir", "-d", default="datas/json_datas", help="Directory where the output JSON is placed")
    parser.add_argument("--input-file", "-i", required=True, help="Input notes JSON file")
    parser.add_argument("--extract-file", help="Existing/target extracted-text JSON file (overrides --output-dir/--output-name)")
    parser.add_argument(
        "--merge-notes",
        action="store_true",
        help="Write a merged notes JSON with note['images'] (url+text) alongside original fields",
    )
    parser.add_argument("--merge-output-file", help="Where to write the merged notes JSON (default: <input>_no_images.json)")
    parser.add_argument(
        "--update-input-file",
        action="store_true",
        help="Update --input-file in place by injecting note['images'] (keeps image_base64)",
    )
    parser.add_argument(
        "--keep-image-base64",
        action="store_true",
        help="Keep image_base64 when writing merged notes output (default: removed, i.e. *_no_images.json style)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.skip_images and API_KEY is None:
        raise SystemExit("set OPENAI_API_KEY before running")

    json_path = Path(args.input_file)
    if not json_path.exists():
        raise SystemExit(f"{json_path} not found; run spider to generate the data first")
    with json_path.open(encoding="utf-8") as f:
        notes = json.load(f)

    if not isinstance(notes, list):
        raise SystemExit(f"{json_path} must contain a JSON list of notes")

    result_path = Path(args.extract_file) if args.extract_file else (Path(args.output_dir) / f"{args.output_name}.json")
    existing: list[dict] = []
    if result_path.exists():
        with result_path.open(encoding="utf-8") as f:
            try:
                loaded = json.load(f)
                existing = loaded if isinstance(loaded, list) else []
            except json.JSONDecodeError:
                existing = []

    worker_count = int(os.environ.get("WORKERS", "3"))
    selected = set(args.note_ids) if args.note_ids else None

    extracted_by_id: dict[str, list[dict]] = {}
    for r in existing:
        if not isinstance(r, dict):
            continue
        note_id = _normalize_note_id(r.get("note_id"))
        if not note_id:
            continue
        extracted_by_id[note_id] = r.get("images") or []

    for note in notes:
        if not isinstance(note, dict):
            continue
        note_id = _normalize_note_id(note.get("note_id"))
        if not note_id:
            continue
        if selected and note_id not in selected:
            continue

        extracted = extracted_by_id.get(note_id, [])
        if not args.skip_images:
            extracted = []
            images = list(enumerate(note.get("image_base64", []) or [], 1))
            if images:
                print(f"[{note_id}] processing {len(images)} image(s) with {worker_count} worker(s)")
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    future_to_idx = {executor.submit(process_image_task, idx, raw_b64): idx for idx, raw_b64 in images}
                    for future in as_completed(future_to_idx):
                        idx = future_to_idx[future]
                        try:
                            text = future.result()
                            extracted.append({"index": idx, "text": text})
                            print(f"[{note_id}] image {idx} done")
                        except Exception as exc:
                            print(f"[{note_id}] image {idx} failed: {exc}")
                            extracted.append({"index": idx, "text": ""})

        extracted.sort(key=lambda x: x["index"])
        if not args.skip_images:
            record = {"note_id": note_id, "images": extracted}
            existing = _upsert_extracted(existing, record)
            extracted_by_id[note_id] = extracted
            write_results(result_path, existing)
            print(f"[{note_id}] saved {len(extracted)} image texts to {result_path}")

        if args.update_input_file:
            note["images"] = _build_images(_normalize_image_list(note.get("image_list")), extracted)
            write_results(json_path, notes)

        if args.merge_notes or args.merge_output_file:
            merged_path = Path(args.merge_output_file) if args.merge_output_file else _default_merged_path(json_path)
            merged_notes: list[dict] = []
            for n in notes:
                if not isinstance(n, dict):
                    continue
                nid = _normalize_note_id(n.get("note_id")) or ""
                merged = dict(n)
                merged["images"] = _build_images(_normalize_image_list(n.get("image_list")), extracted_by_id.get(nid, []))
                if not args.keep_image_base64 and "image_base64" in merged:
                    merged.pop("image_base64", None)
                merged_notes.append(merged)
            write_results(merged_path, merged_notes)
            print(f"[{note_id}] updated merged notes JSON at {merged_path}")

    print("All notes processed.")


if __name__ == "__main__":
    main()
