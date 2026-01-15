import argparse
import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class RowSpec:
    kind: str  # "tweet" | "reply"
    date_col: str
    url_col: str
    text_col: str


TWEET_SPECS: List[RowSpec] = [
    RowSpec(kind="tweet", date_col="Tweet Date", url_col="Tweet URL", text_col="Tweet Content"),
]

REPLY_SPECS: List[RowSpec] = [
    RowSpec(kind="reply", date_col="Reply Date", url_col="Reply URL", text_col="Reply Content"),
]


def _find_header_row(rows: Iterable[List[str]]) -> Tuple[Optional[List[str]], List[List[str]]]:
    """
    Returns (header, buffered_rows_after_header).
    Skips metadata rows until a known header is found.
    """
    buffered: List[List[str]] = []
    for row in rows:
        if not row:
            continue
        buffered.append(row)
        cols = set(row)
        if ("Tweet URL" in cols and "Tweet Content" in cols) or ("Reply URL" in cols and "Reply Content" in cols):
            return row, []
    return None, buffered


def _spec_from_header(header: List[str], mode: str) -> Optional[RowSpec]:
    specs: List[RowSpec] = []
    if mode in ("all", "tweets"):
        specs.extend(TWEET_SPECS)
    if mode in ("all", "replies"):
        specs.extend(REPLY_SPECS)

    header_set = set(header)
    for spec in specs:
        if spec.date_col in header_set and spec.url_col in header_set and spec.text_col in header_set:
            return spec
    return None


def _iter_csv_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            yield row


def _discover_csv_files(inputs: List[str], root: str) -> List[Path]:
    files: List[Path] = []
    if inputs:
        for p in inputs:
            path = Path(p)
            if path.is_dir():
                files.extend(sorted(path.rglob("*.csv")))
            elif any(ch in p for ch in ["*", "?", "["]):
                files.extend(sorted(Path().glob(p)))
            else:
                files.append(path)
        return [p for p in files if p.is_file() and p.suffix.lower() == ".csv"]

    root_path = Path(root)
    ignore = {".git", ".venv", "__pycache__", "twitter"}
    for p in root_path.rglob("*.csv"):
        if any(part in ignore for part in p.parts):
            continue
        files.append(p)
    return sorted(files)


def extract_simple_rows(
    csv_files: List[Path],
    mode: str,
    dedupe: bool,
) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    seen = set()

    for path in csv_files:
        try:
            rows = _iter_csv_rows(path)
            header, _ = _find_header_row(rows)
            if not header:
                continue
            spec = _spec_from_header(header, mode=mode)
            if not spec:
                continue

            date_idx = header.index(spec.date_col)
            url_idx = header.index(spec.url_col)
            text_idx = header.index(spec.text_col)

            for row in _iter_csv_rows(path):
                # Skip metadata until we hit header again (cheap, robust)
                if row == header:
                    continue
                if not row or len(row) <= max(date_idx, url_idx, text_idx):
                    continue

                date = str(row[date_idx]).strip()
                url = str(row[url_idx]).strip()
                text = str(row[text_idx]).strip()
                if not (date or url or text):
                    continue

                key = (url, text) if dedupe else None
                if dedupe and key in seen:
                    continue
                if dedupe:
                    seen.add(key)
                out.append((date, url, text))
        except Exception:
            # best-effort: skip bad CSVs
            continue

    return out


def main():
    parser = argparse.ArgumentParser(
        description="Extract crawled Twitter/X content into a simple file (CSV/JSON/JSONL): Date, URL, Text."
    )
    parser.add_argument("inputs", nargs="*", help="CSV file/dir/glob. If empty, scan --root recursively.")
    parser.add_argument("--root", default=".", help="Root directory to scan when no inputs are provided.")
    parser.add_argument(
        "--mode",
        choices=["tweets", "replies", "all"],
        default="tweets",
        help="Which CSV rows to extract.",
    )
    parser.add_argument(
        "--no-dedupe",
        action="store_true",
        help="Keep duplicate rows (media CSVs often repeat the same tweet URL).",
    )
    parser.add_argument(
        "--format",
        choices=["auto", "csv", "json", "jsonl"],
        default="auto",
        help="Output format. 'auto' infers from -o extension (.json/.jsonl), otherwise defaults to CSV.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON (only when --format json).",
    )
    parser.add_argument("-o", "--output", default=None, help="Output path. Default depends on --format.")
    args = parser.parse_args()

    csv_files = _discover_csv_files(args.inputs, root=args.root)
    rows = extract_simple_rows(csv_files, mode=args.mode, dedupe=not args.no_dedupe)

    out_path = Path(args.output) if args.output else None
    if args.format == "auto":
        if out_path and out_path.suffix.lower() == ".jsonl":
            out_format = "jsonl"
        elif out_path and out_path.suffix.lower() == ".json":
            out_format = "json"
        else:
            out_format = "csv"
    else:
        out_format = args.format

    if out_path is None:
        out_path = Path(
            "exported_content.jsonl"
            if out_format == "jsonl"
            else "exported_content.json"
            if out_format == "json"
            else "exported_content.csv"
        )

    out_dir = out_path.parent
    if str(out_dir) and str(out_dir) != ".":
        os.makedirs(out_dir, exist_ok=True)

    if out_format == "csv":
        with out_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Date", "URL", "Text"])
            w.writerows(rows)
    elif out_format == "jsonl":
        with out_path.open("w", encoding="utf-8", newline="\n") as f:
            for date, url, text in rows:
                f.write(
                    json.dumps({"date": date, "url": url, "text": text}, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
    elif out_format == "json":
        payload = [{"date": date, "url": url, "text": text} for date, url, text in rows]
        with out_path.open("w", encoding="utf-8", newline="\n") as f:
            if args.pretty:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            else:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
    else:
        raise SystemExit(f"Unsupported --format: {out_format}")

    print(f"Input CSV files: {len(csv_files)}")
    print(f"Extracted rows: {len(rows)}")
    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    main()
