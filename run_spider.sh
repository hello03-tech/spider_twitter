#!/usr/bin/env bash
set -euo pipefail

QUERY="gemini 3 前端"
COUNT=10
EXTRACT_IMAGES=1
NOTE_IDS=""
OUTPUT_NAME=""
WORKERS=32

DEFAULT_OPENAI_BASE_URL="https://api.openai.com/v1"
DEFAULT_MODEL="gemini-2.5-flash"
# Do not hardcode secrets here; set via environment variables.
#   export OPENAI_API_KEY=...
#   export OPENAI_BASE_URL=...   (optional)

LATEST=0
TEXT=0
QUIET=0

print_usage() {
  cat <<'EOF'
Usage: ./run_spider.sh [options]

Options:
  -q, --query QUERY         specify the search keyword/filter (default: UI style)
  -n, --count COUNT         approx total results to process (default: 200)
  --latest                  use [Latest] tab (default: [Media])
  --text                    text-only mode (consumes lots of API calls)
  --quiet                   disable progress output
  --extract-images          call Gemini to extract image texts (default: on)
  --no-extract-images       skip Gemini image text extraction
  --note-ids ID1,ID2        only extract images for these tweet IDs (comma-separated)
  --output-name NAME        override output name prefix (default: <query>_<timestamp>)
  --workers N               concurrency (Twitter requests + OCR workers) (default: 32)
  -h, --help                show this message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -q|--query)
      QUERY="$2"
      shift 2
      ;;
    -n|--count)
      COUNT="$2"
      shift 2
      ;;
    --latest)
      LATEST=1
      shift
      ;;
    --text)
      TEXT=1
      shift
      ;;
    --quiet)
      QUIET=1
      shift
      ;;
    --extract-images)
      EXTRACT_IMAGES=1
      shift
      ;;
    --no-extract-images)
      EXTRACT_IMAGES=0
      shift
      ;;
    --note-ids)
      NOTE_IDS="$2"
      shift 2
      ;;
    --output-name)
      OUTPUT_NAME="$2"
      shift 2
      ;;
    --workers)
      WORKERS="$2"
      shift 2
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      print_usage
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f "settings.json" ]]; then
  echo "Error: settings.json not found in $SCRIPT_DIR" >&2
  exit 1
fi

OPENAI_BASE_URL="${OPENAI_BASE_URL:-$DEFAULT_OPENAI_BASE_URL}"
OPENAI_API_KEY="${OPENAI_API_KEY:?set OPENAI_API_KEY before running}"
MODEL="${MODEL:-$DEFAULT_MODEL}"

if [[ -z "$OUTPUT_NAME" ]]; then
  timestamp=$(date +%Y%m%d%H%M%S)
  safe_query="${QUERY// /_}"
  safe_query="${safe_query//\//_}"
  OUTPUT_NAME="${safe_query}_${timestamp}"
fi

PYTHON_BIN="./.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

extra_args=()
if [[ "$LATEST" -eq 1 ]]; then
  extra_args+=(--latest)
fi
if [[ "$TEXT" -eq 1 ]]; then
  extra_args+=(--text)
fi
if [[ "$QUIET" -eq 1 ]]; then
  extra_args+=(--quiet)
fi

echo "Launching twitter_download with query='$QUERY', count=$COUNT, workers=$WORKERS, output='$OUTPUT_NAME'"

WORKERS="$WORKERS" \
"$PYTHON_BIN" main.py --search "$QUERY" --count "$COUNT" --format json --folder "$OUTPUT_NAME" --workers "$WORKERS" --no-media "${extra_args[@]}"

JSON_DIR="datas/json_datas"
OUTPUT_FILE="$JSON_DIR/$OUTPUT_NAME.json"
NO_IMG_FILE="$JSON_DIR/${OUTPUT_NAME}_no_images.json"
WITH_B64_FILE="$JSON_DIR/${OUTPUT_NAME}_with_image_base64.json"

# By default we omit image_base64 so the main output matches Spider_XHS *_no_images.json style.
"$PYTHON_BIN" twitter_to_spider_json.py --settings "settings.json" --folder "$OUTPUT_NAME" --output-dir "$JSON_DIR" --output-name "$OUTPUT_NAME"

if [[ -f "$OUTPUT_FILE" ]]; then
  "$PYTHON_BIN" - <<PY
import json, os
source = os.path.abspath(os.path.join("$OUTPUT_FILE"))
target = os.path.abspath(os.path.join("$NO_IMG_FILE"))
with open(source, "r", encoding="utf-8") as f:
    data = json.load(f)
sanitized = []
for note in data:
    note_copy = {k: v for k,v in note.items() if k != "image_base64"}
    sanitized.append(note_copy)
with open(target, "w", encoding="utf-8") as f:
    json.dump(sanitized, f, ensure_ascii=False, indent=2)
print(f"Saved image-free JSON to {target}")
PY
else
  echo "Error: expected JSON output $OUTPUT_FILE not found" >&2
  exit 1
fi

if [[ "$EXTRACT_IMAGES" -eq 1 ]]; then
  # For OCR we need the local image bytes; generate a separate file that includes image_base64.
  "$PYTHON_BIN" twitter_to_spider_json.py --settings "settings.json" --folder "$OUTPUT_NAME" --output-dir "$JSON_DIR" --output-name "${OUTPUT_NAME}_with_image_base64" --include-image-base64
  if [[ ! -f "$WITH_B64_FILE" ]]; then
    echo "Error: expected JSON output $WITH_B64_FILE not found" >&2
    exit 1
  fi

  BASE64_INPUT_FILE="$WITH_B64_FILE"
  # analyze_styles.py consumes note["image_base64"]. If it's empty (e.g. media files not saved locally),
  # fall back to downloading note["image_list"] URLs to build image_base64, like Spider_XHS.
  if "$PYTHON_BIN" - <<PY
import json
from pathlib import Path

p = Path("$WITH_B64_FILE")
with p.open(encoding="utf-8") as f:
    notes = json.load(f)
has_any = any(isinstance(n, dict) and (n.get("image_base64") or []) for n in notes)
raise SystemExit(0 if has_any else 1)
PY
  then
    :
  else
    BASE64_INPUT_FILE="$JSON_DIR/${OUTPUT_NAME}_with_base64.json"
    echo "Preparing $BASE64_INPUT_FILE (adding image_base64 via downloads) ..."
    "$PYTHON_BIN" - <<PY
import base64
import json
import socket
import urllib.request
from pathlib import Path

socket.setdefaulttimeout(20)

src = Path("$OUTPUT_FILE")
dst = Path("$BASE64_INPUT_FILE")

with src.open(encoding="utf-8") as f:
    notes = json.load(f)

def fetch_b64(url: str):
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
    return base64.b64encode(raw).decode("utf-8")

out = []
for note in notes:
    if not isinstance(note, dict):
        out.append(note)
        continue
    urls = note.get("image_list") or []
    b64_list = []
    for u in urls:
        try:
            b64 = fetch_b64(u)
        except Exception:
            b64 = None
        if b64:
            b64_list.append(b64)
    note2 = dict(note)
    note2["image_base64"] = b64_list
    out.append(note2)

dst.parent.mkdir(parents=True, exist_ok=True)
tmp = dst.with_suffix(".tmp")
with tmp.open("w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
tmp.replace(dst)
print(f"Wrote {dst}")
PY
  fi

  echo "Running Gemini OCR to extract image text..."
  note_args=()
  if [[ -n "$NOTE_IDS" ]]; then
    IFS=',' read -r -a parsed_ids <<< "$NOTE_IDS"
    note_args+=(--note-ids "${parsed_ids[@]}")
  fi
  EXTRACT_OUTPUT_NAME="${OUTPUT_NAME}_extract_texts"
  EXTRACT_OUTPUT_FILE="$JSON_DIR/${EXTRACT_OUTPUT_NAME}.json"
  note_args+=(--output-dir "$JSON_DIR" --output-name "$EXTRACT_OUTPUT_NAME" --input-file "$BASE64_INPUT_FILE")

  OPENAI_BASE_URL="$OPENAI_BASE_URL" \
  OPENAI_API_KEY="$OPENAI_API_KEY" \
  MODEL="$MODEL" \
  WORKERS="$WORKERS" \
  "$PYTHON_BIN" analyze_styles.py "${note_args[@]}"

  "$PYTHON_BIN" - <<PY
import json
from pathlib import Path

notes_path = Path("$OUTPUT_FILE")
extract_path = Path("$EXTRACT_OUTPUT_FILE")

with notes_path.open(encoding="utf-8") as f:
    notes = json.load(f)
with extract_path.open(encoding="utf-8") as f:
    extracted = json.load(f)

by_id = {}
for r in extracted:
    if isinstance(r, dict) and r.get("note_id"):
        by_id[r["note_id"]] = r.get("images") or []

for n in notes:
    if not isinstance(n, dict):
        continue
    note_id = n.get("note_id")
    if not note_id:
        continue
    img_urls = n.get("image_list") or []
    img_texts = by_id.get(note_id)
    if img_texts is None:
        continue
    images = []
    if img_urls:
        for i, url in enumerate(img_urls, 1):
            images.append({"index": i, "url": url, "text": ""})
        for item in img_texts:
            try:
                idx = int(item.get("index"))
            except Exception:
                continue
            if 1 <= idx <= len(images):
                images[idx - 1]["text"] = item.get("text", "") or ""
            else:
                images.append({"index": idx, "url": "", "text": item.get("text", "") or ""})
    else:
        for item in img_texts:
            images.append({"index": item.get("index"), "url": "", "text": item.get("text", "") or ""})
    n["images"] = images

tmp = notes_path.with_suffix(".tmp")
with tmp.open("w", encoding="utf-8") as f:
    json.dump(notes, f, ensure_ascii=False, indent=2)
tmp.replace(notes_path)
print(f"Wrote image texts into {notes_path} under note['images']")

no_img_path = Path("$NO_IMG_FILE")
if no_img_path.exists():
    sanitized = []
    for n in notes:
        if isinstance(n, dict) and "image_base64" in n:
            n = {k: v for k, v in n.items() if k != "image_base64"}
        sanitized.append(n)
    tmp2 = no_img_path.with_suffix(".tmp")
    with tmp2.open("w", encoding="utf-8") as f:
        json.dump(sanitized, f, ensure_ascii=False, indent=2)
    tmp2.replace(no_img_path)
    print(f"Updated {no_img_path} (kept note['images'], removed image_base64)")
PY
fi

echo "Done."
echo "Output: $OUTPUT_FILE"
echo "No-images: $NO_IMG_FILE"
