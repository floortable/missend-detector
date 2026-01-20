#!/usr/bin/env python3
import argparse
import json
import os
import re
from pathlib import Path


DEFAULT_SEPARATOR_PATTERN = r"^ー+$"
DEFAULT_QUESTION_KEYWORD = "QUESTION"
DEFAULT_ANSWER_KEYWORD = "ANSWER"
DEFAULT_HEADER_DATE_PATTERN = r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}"


from env_loader import load_dotenv


def build_patterns():
    separator_pattern = os.environ.get("FIELD_SEPARATOR_PATTERN", DEFAULT_SEPARATOR_PATTERN)
    question_keyword = os.environ.get("QUESTION_KEYWORD", DEFAULT_QUESTION_KEYWORD)
    answer_keyword = os.environ.get("ANSWER_KEYWORD", DEFAULT_ANSWER_KEYWORD)
    header_date_pattern = os.environ.get("HEADER_DATE_PATTERN", DEFAULT_HEADER_DATE_PATTERN)

    separator_re = re.compile(separator_pattern)
    type_pattern = rf"{re.escape(question_keyword)}|{re.escape(answer_keyword)}"
    header_re = re.compile(
        rf"(?P<date>{header_date_pattern}).*?\b(?P<type>{type_pattern})\b",
        re.IGNORECASE,
    )
    return separator_re, header_re, question_keyword, answer_keyword


def parse_entries(text, separator_re, header_re, question_keyword, answer_keyword):
    lines = [line.rstrip("\n") for line in text.splitlines()]
    entries = []
    i = 0

    while i < len(lines):
        if not separator_re.match(lines[i]):
            i += 1
            continue

        i += 1
        while i < len(lines) and lines[i] == "":
            i += 1
        if i >= len(lines):
            break

        header = lines[i]
        header_match = header_re.search(header)
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
            continue

        entry_type_raw = header_match.group("type")
        entry_type = (
            "Question"
            if entry_type_raw.upper() == question_keyword.upper()
            else "Answer"
        )
        data = "\n".join(content_lines).strip()
        entries.append(
            {
                "date": header_match.group("date"),
                "type": entry_type,
                "data": data,
            }
        )

    return entries


def main():
    load_dotenv()
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

    input_path = Path(args.input)
    case_id = args.case_id
    if not case_id:
        match = re.fullmatch(r"(\d{8})\.txt", input_path.name)
        if match:
            case_id = match.group(1)
    if not case_id:
        raise SystemExit("Case IDを特定できません。--case-idを指定してください。")
    text = input_path.read_text(encoding="utf-8")
    separator_re, header_re, question_keyword, answer_keyword = build_patterns()
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
