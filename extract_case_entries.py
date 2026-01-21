#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
from pathlib import Path


DEFAULT_SEPARATOR_PATTERN = r"^ー+$"
DEFAULT_QUESTION_KEYWORD = "QUESTION"
DEFAULT_ANSWER_KEYWORD = "ANSWER"
DEFAULT_HEADER_DATE_PATTERN = r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}"


from env_loader import load_dotenv


def normalize_keywords(value, default_value):
    raw = value or default_value
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_patterns():
    separator_pattern = os.environ.get("FIELD_SEPARATOR_PATTERN", DEFAULT_SEPARATOR_PATTERN)
    question_keywords = normalize_keywords(
        os.environ.get("QUESTION_KEYWORD"), DEFAULT_QUESTION_KEYWORD
    )
    answer_keywords = normalize_keywords(
        os.environ.get("ANSWER_KEYWORD"), DEFAULT_ANSWER_KEYWORD
    )
    header_date_pattern = os.environ.get("HEADER_DATE_PATTERN", DEFAULT_HEADER_DATE_PATTERN)

    separator_re = re.compile(separator_pattern)
    type_keywords = question_keywords + answer_keywords
    type_pattern = "|".join(re.escape(keyword) for keyword in type_keywords)
    header_re = re.compile(
        rf"(?P<date>{header_date_pattern}).*?(?P<type>{type_pattern})",
        re.IGNORECASE,
    )
    return separator_re, header_re, question_keywords, answer_keywords


def parse_entries(text, separator_re, header_re, question_keyword, answer_keyword):
    lines = [line.rstrip("\r\n") for line in text.splitlines()]
    entries = []
    i = 0
    separator_hits = 0
    header_hits = 0
    header_misses = []

    while i < len(lines):
        if not separator_re.match(lines[i]):
            i += 1
            continue

        separator_hits += 1
        i += 1
        while i < len(lines) and lines[i] == "":
            i += 1
        if i >= len(lines):
            break

        header = lines[i]
        header_norm = header.replace("\u3000", " ")
        header_match = header_re.search(header_norm)
        i += 1

        while i < len(lines) and not separator_re.match(lines[i]):
            i += 1
        if i >= len(lines):
            break

        i += 1
        content_lines = []
        while i < len(lines) and not separator_re.match(lines[i]):
            content_lines.append(lines[i])
            i += 1

        if not header_match:
            if len(header_misses) < 5:
                header_misses.append(header)
            continue

        header_hits += 1
        entry_type_raw = header_match.group("type")
        entry_type = "Unknown"
        for keyword in question_keyword:
            if entry_type_raw.lower() == keyword.lower():
                entry_type = "Question"
                break
        if entry_type == "Unknown":
            for keyword in answer_keyword:
                if entry_type_raw.lower() == keyword.lower():
                    entry_type = "Answer"
                    break
        data = "\n".join(content_lines).strip()
        entries.append(
            {
                "date": header_match.group("date"),
                "type": entry_type,
                "data": data,
            }
        )

    logging.debug(
        "parse_entries: lines=%s separator_hits=%s header_hits=%s entries=%s",
        len(lines),
        separator_hits,
        header_hits,
        len(entries),
    )
    if header_misses:
        logging.debug("ヘッダー判定に失敗した例: %s", header_misses)
    return entries


def main():
    load_dotenv()
    if os.environ.get("LOG_ENABLED", "true").lower() in {"1", "true", "yes"}:
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)s %(message)s",
        )
    else:
        logging.disable(logging.CRITICAL)
    case_id_digits = int(os.environ.get("CASE_ID_DIGITS", "8") or "8")
    parser = argparse.ArgumentParser(
        description="Case IDのテキストからQUESTION/ANSWERを抽出してJSON出力します。"
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=str(Path(__file__).resolve().parent / "work" / "00000000.txt"),
        help="入力ファイル (default: ./work/00000000.txt)",
    )
    parser.add_argument(
        "--case-id",
        help="Case ID (未指定の場合は入力ファイル名から推測)",
    )
    parser.add_argument(
        "--output",
        help="出力ファイル。未指定の場合は標準出力。",
    )
    args = parser.parse_args()

    case_id = args.case_id
    input_path = Path(args.input)
    if args.input == parser.get_default("input") and case_id:
        work_dir = Path(os.environ.get("WORK_DIR", Path(__file__).resolve().parent / "work"))
        input_path = work_dir / f"{case_id}.txt"
    if not case_id:
        match = re.fullmatch(rf"(\\d{{{case_id_digits}}})\\.txt", input_path.name)
        if match:
            case_id = match.group(1)
    if not case_id:
        raise SystemExit("Case IDを特定できません。--case-idを指定してください。")
    text = input_path.read_text(encoding="utf-8")
    logging.debug("入力ファイル: %s 文字数=%s", input_path, len(text))
    separator_re, header_re, question_keyword, answer_keyword = build_patterns()
    logging.debug(
        "separator_pattern=%r header_pattern=%r question_keyword=%r answer_keyword=%r",
        separator_re.pattern,
        header_re.pattern,
        question_keyword,
        answer_keyword,
    )
    entries = parse_entries(text, separator_re, header_re, question_keyword, answer_keyword)

    output_text = json.dumps(entries, ensure_ascii=False, indent=4)
    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
    else:
        work_dir = Path(os.environ.get("WORK_DIR", Path(__file__).resolve().parent / "work"))
        work_dir.mkdir(parents=True, exist_ok=True)
        output_path = work_dir / f"{case_id}.json"
        output_path.write_text(output_text, encoding="utf-8")
        print(output_text)


if __name__ == "__main__":
    main()
