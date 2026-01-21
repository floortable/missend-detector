#!/usr/bin/env python3
import json
import logging
import os
import re
import time
import signal
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright

from extract_case_entries import build_patterns, parse_entries
from env_loader import load_dotenv


CASE_ID_RE = re.compile(r"^(?P<case_id>\d{8})\.txt$")
META_LINE_RE = re.compile(r"^(【.*】|\[.*\])$")
LOG_LINE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|INFO|ERROR|DEBUG|TRACE|WARN|WARNING)\b"
)
JSON_LINE_RE = re.compile(r"^\s*[{[].*[}\]]\s*$")
DEFAULT_LLM_PROMPT = """あなたはサポートチケットの内容整合性を確認するAIです。

入力として、ある案件（チケット）に関する履歴が時系列順に与えられます。
各履歴は以下の構造を持ちます：
- type: question (質問) または answer (回答)
- created_on: 作成日時
- text: 質問または回答の本文とコメント（ログやノイズは削除済み）

あなたの任務は、「最後の回答（type=answer）」が
本当にこの案件の直近の質問（type=question）に対する
文脈的に正しい回答であるかどうかを判定することです。

### 判定のポイント：
- 内容の正確性・品質は評価しない（例：回答が正しいかどうかは無関係）。
- あくまで **話の流れ・文脈の整合性** のみを判断する。
- 「別案件の話題」「全く異なるテーマ」「明らかに関係ない文脈」なら取り違えの可能性あり。
- 受付番号などのIDや案件名の判定はすでに前処理済み。ここでは回答の内容のみ、同案件の内容であるかのみ判断する。

### 出力フォーマット：
必ず以下の形式で出力してください：

査閲結果：<承認|却下|不明>
理由：<客観的な理由>

#### 定義：
- **承認**：最後の回答が、同じ案件に関する質問に自然に対応している。
- **却下**：最後の回答が、異なる案件・別テーマ・文脈の異なる質問に対応している。
- **不明**：情報が少なすぎる・文脈が判断できない。

### 履歴
{entries}
"""

STOP_REQUESTED = False
FORCE_STOP = False


def handle_stop_signal(signum, _frame):
    global STOP_REQUESTED, FORCE_STOP
    if STOP_REQUESTED:
        FORCE_STOP = True
        logging.error("強制停止シグナル(%s)を受信しました。即時終了します。", signum)
        sys.exit(1)
    STOP_REQUESTED = True
    logging.info("停止シグナル(%s)を受信しました。現在の処理が終わり次第停止します。", signum)


def build_url(base_url, case_id):
    if "?" in base_url or base_url.endswith("="):
        return f"{base_url}{case_id}"
    base = base_url if base_url.endswith("/") else base_url + "/"
    return urljoin(base, case_id)


def normalize_url(url):
    return url.rstrip("/")


def login_if_needed(page, login_url, username, password, selectors):
    if normalize_url(page.url).startswith(normalize_url(login_url)):
        page.fill(selectors["username"], username)
        page.fill(selectors["password"], password)
        page.click(selectors["submit"])
        try:
            page.wait_for_url(
                lambda url: not normalize_url(url).startswith(normalize_url(login_url)),
                timeout=30000,
            )
        except Exception:
            pass
        page.wait_for_load_state("load")


def collect_page_content(page):
    parts = []
    main_url = page.url
    parts.append(f"<!-- main frame url={main_url} -->")
    parts.append(page.content())
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        frame_url = frame.url or "about:blank"
        if frame_url == "about:blank":
            continue
        try:
            frame_content = frame.content()
        except Exception as exc:
            logging.debug("フレーム内容の取得に失敗しました: %s (%s)", frame_url, exc)
            continue
        parts.append(f"<!-- frame url={frame_url} -->")
        parts.append(frame_content)
    return "\n".join(parts)


def collect_visible_text(page):
    def extract_text(frame):
        try:
            return frame.evaluate(
                """
                () => {
                  const parts = [];
                  const walk = (node) => {
                    if (!node) return;
                    if (node.nodeType === Node.TEXT_NODE) {
                      const text = node.textContent && node.textContent.trim();
                      if (text) parts.push(text);
                      return;
                    }
                    if (
                      node.nodeType !== Node.ELEMENT_NODE &&
                      node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE
                    ) {
                      return;
                    }
                    if (node.shadowRoot) walk(node.shadowRoot);
                    for (const child of node.childNodes || []) {
                      walk(child);
                    }
                  };
                  walk(document.body || document.documentElement);
                  return parts.join("\\n");
                }
                """
            )
        except Exception as exc:
            logging.debug("テキスト抽出に失敗しました: %s (%s)", frame.url, exc)
            return ""

    parts = []
    main_text = extract_text(page.main_frame)
    if main_text:
        parts.append(f"<!-- main frame url={page.url} -->")
        parts.append(main_text)
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        frame_text = extract_text(frame)
        if frame_text:
            frame_url = frame.url or "about:blank"
            parts.append(f"<!-- frame url={frame_url} -->")
            parts.append(frame_text)
    return "\n".join(parts)


def fetch_case_text(case_id, base_url, work_dir, browser_settings, login_settings):
    url = build_url(base_url, case_id)
    output_path = work_dir / f"{case_id}.txt"

    launch_args = []
    if browser_settings["profile_dir"]:
        launch_args.append(f"--profile-directory={browser_settings['profile_dir']}")

    selectors = login_settings["selectors"]

    # ログイン済みのChromeプロファイルを使える場合は永続コンテキストを使う。
    with sync_playwright() as p:
        if browser_settings["user_data_dir"]:
            context = p.chromium.launch_persistent_context(
                user_data_dir=browser_settings["user_data_dir"],
                channel=browser_settings["channel"],
                headless=browser_settings["headless"],
                args=launch_args,
            )
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = p.chromium.launch(
                channel=browser_settings["channel"],
                headless=browser_settings["headless"],
                args=launch_args,
            )
            context = browser.new_context()
            page = context.new_page()

        try:
            page.goto(url, wait_until="load", timeout=30000)
            login_if_needed(
                page,
                login_url=login_settings["url"],
                username=login_settings["username"],
                password=login_settings["password"],
                selectors=selectors,
            )
            if normalize_url(page.url).startswith(normalize_url(login_settings["url"])):
                page.goto(url, wait_until="load", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                logging.debug("networkidle待機がタイムアウトしました。")
            if browser_settings["wait_seconds"] > 0:
                logging.info("ページ表示後に%s秒待機します。", browser_settings["wait_seconds"])
                page.wait_for_timeout(browser_settings["wait_seconds"] * 1000)
            page_html = collect_page_content(page)
            page_text = collect_visible_text(page)
            if not page_text.strip() and page_html.strip():
                logging.info("テキストが空のためHTMLを保存します。")
            body_text = page_text.strip() or page_html
            logging.debug(
                "取得内容: html_chars=%s text_chars=%s",
                len(page_html),
                len(page_text),
            )
        finally:
            if browser_settings["keep_open"]:
                logging.info("ブラウザを閉じる前に待機します。Enterで終了します。")
                try:
                    input()
                except EOFError:
                    pass
            context.close()

    output_path.write_text(body_text, encoding="utf-8")
    return output_path


def clean_entry_data(text):
    # 見出しやラベルなどのメタ行を除去して本文だけ残す。
    cleaned = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if META_LINE_RE.match(stripped):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def remove_logs(text, log_filter):
    if not text:
        return ""
    max_line_len = log_filter["max_line_len"]
    removed = 0
    filtered = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if LOG_LINE_RE.match(stripped):
            removed += 1
            continue
        if JSON_LINE_RE.match(stripped):
            removed += 1
            continue
        if len(stripped) > max_line_len:
            removed += 1
            continue
        filtered.append(line)
    logging.debug("log_filter: removed=%s kept=%s", removed, len(filtered))
    return "\n".join(filtered).strip()


def trim_entries(entries, max_chars):
    # 既に新しい順なので、文字数上限まで順に詰める。
    trimmed = []
    total = 0
    for entry in entries:
        data = entry["data"]
        if not data:
            continue
        if total >= max_chars:
            break
        remaining = max_chars - total
        if len(data) > remaining:
            data = data[:remaining]
        trimmed.append({**entry, "data": data})
        total += len(data)
        if total >= max_chars:
            break
    return trimmed


def build_case_json(case_text, max_chars, log_filter):
    # 抽出→整形→LLMに渡すサイズまで切り詰める。
    separator_re, header_re, question_keyword, answer_keyword = build_patterns()
    entries = parse_entries(case_text, separator_re, header_re, question_keyword, answer_keyword)
    cleaned_entries = []
    for entry in entries:
        original_data = entry["data"]
        cleaned = clean_entry_data(original_data)
        if not cleaned and original_data:
            logging.debug(
                "clean_entry_dataで全削除されたため元の内容を保持します: type=%s",
                entry["type"],
            )
            cleaned = original_data
        original_cleaned = cleaned
        if log_filter["enabled"]:
            cleaned = remove_logs(cleaned, log_filter)
            if not cleaned and original_cleaned:
                logging.debug(
                    "log_filterで全削除されたため元の内容を保持します: type=%s",
                    entry["type"],
                )
                cleaned = original_cleaned
        if not cleaned:
            logging.debug("空エントリのため除外します: type=%s", entry["type"])
            continue
        logging.debug("entry cleaned: type=%s chars=%s", entry["type"], len(cleaned))
        cleaned_entries.append({**entry, "data": cleaned})
    return trim_entries(cleaned_entries, max_chars)


def build_llm_url(base_url):
    # ベースURL/フルパスのどちらでも受け付ける。
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def call_llm(case_id, entries_payload, settings):
    prompt_template = settings["prompt"] or DEFAULT_LLM_PROMPT
    # {entries} 置換が使えるようにテンプレート形式を維持。
    if "{entries}" not in prompt_template:
        print("WARNING: LLM_PROMPTに{entries}が含まれていません。", flush=True)
    prompt = prompt_template.replace("{entries}", entries_payload)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Case ID: {case_id} の判定をお願いします。"},
    ]

    request_body = {
        "model": settings["model"],
        "messages": messages,
        "temperature": settings["temperature"],
    }

    headers = {"Content-Type": "application/json"}
    if settings["api_key"]:
        headers["Authorization"] = f"Bearer {settings['api_key']}"

    cert_file = settings.get("cert_file") or None
    response = requests.post(
        build_llm_url(settings["base_url"]),
        headers=headers,
        json=request_body,
        timeout=settings["timeout"],
        cert=cert_file,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def parse_llm_json(text):
    # 前後に余計な文があってもJSONだけ拾えるようにする。
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None


def parse_llm_judgement(text):
    # 既定プロンプトの日本語フォーマットに対応。
    result_match = re.search(r"査閲結果：\s*(承認|却下|不明)", text)
    reason_match = re.search(r"理由：\s*(.+)", text)
    result = result_match.group(1) if result_match else None
    reason = reason_match.group(1).strip() if reason_match else None
    return result, reason


def notify_teams(case_id, llm_text, llm_json, webhook_urls):
    if not webhook_urls:
        return
    if isinstance(webhook_urls, str):
        webhook_urls = [webhook_urls]
    webhook_urls = [url for url in webhook_urls if url]
    if not webhook_urls:
        return
    result, reason = parse_llm_judgement(llm_text)
    # 不一致アラートは専用のサマリーを使う。
    summary = f"Case ID {case_id} {result or ''}".strip()
    if result == "却下":
        summary = f"Case ID {case_id} caseid mismatch"
    card_body = build_adaptive_card_body(
        case_id=case_id,
        result=result or "不明",
        reason=reason,
        llm_text=llm_text,
    )
    send_adaptive_card(webhook_urls, card_body, summary=summary)


def build_adaptive_card_body(case_id, result, reason, llm_text):
    # 既存の通知レイアウトに合わせてカードを組み立てる。
    case_url = build_url(os.environ.get("BASE_URL", "http://localhost:8080/"), case_id)
    if result == "却下":
        return [
            {
                "type": "Container",
                "style": "attention",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": "🚨 受付番号不一致の可能性",
                        "size": "Large",
                        "weight": "Bolder",
                        "color": "Attention",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": f"[Case #{case_id}]({case_url})",
                        "wrap": True,
                        "spacing": "Small",
                    },
                    {
                        "type": "TextBlock",
                        "text": "LLMが caseid mismatch を検知しました。異なる受付番号への回答が申告されています。至急確認してください。",
                        "wrap": True,
                        "spacing": "Medium",
                        "color": "Attention",
                    },
                    {
                        "type": "TextBlock",
                        "text": f"理由：{reason or llm_text}",
                        "wrap": True,
                        "spacing": "Small",
                    },
                ],
                "bleed": True,
            }
        ]
    if result == "承認":
        emoji = "✅"
        items = [
            {
                "type": "TextBlock",
                "text": f"{emoji} **チケット承認**",
                "size": "Large",
                "weight": "Bolder",
                "color": "Good",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": f"[Case #{case_id}]({case_url})",
                "wrap": True,
                "spacing": "Small",
            },
        ]
        if reason:
            items.append(
                {"type": "TextBlock", "text": f"理由：{reason}", "wrap": True}
            )
        else:
            items.append({"type": "TextBlock", "text": llm_text, "wrap": True})
        return [{"type": "Container", "items": items, "bleed": True}]

    emoji = "❔"
    return [
        {
            "type": "Container",
            "items": [
                {
                    "type": "TextBlock",
                    "text": f"{emoji} 判定不明",
                    "size": "Large",
                    "weight": "Bolder",
                    "wrap": True,
                },
                {
                    "type": "TextBlock",
                    "text": f"[Case #{case_id}]({case_url})",
                    "wrap": True,
                    "spacing": "Small",
                },
                {
                    "type": "TextBlock",
                    "text": llm_text,
                    "wrap": True,
                },
            ],
        }
    ]


def send_adaptive_card(webhooks, body, summary, success_label=None):
    # Teams向けのAdaptive Cardとして送信する。
    card = {
        "type": "message",
        "summary": summary,
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body,
                },
            }
        ],
    }
    if success_label:
        card["summary"] = f"{summary} ({success_label})"

    for webhook in webhooks:
        if not webhook:
            continue
        requests.post(webhook, json=card, timeout=10)


def wait_for_stable_size(path, retries=5, interval=1.0):
    # 書き込み中のファイルを読まないようにする。
    last_size = -1
    for _ in range(retries):
        if STOP_REQUESTED:
            return False
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last_size:
            return True
        last_size = size
        time.sleep(interval)
    logging.debug("ファイルサイズが安定しませんでした: %s", path)
    return True


def process_case(case_id, settings):
    work_dir = settings["work_dir"]
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        case_text_path = fetch_case_text(
            case_id,
            base_url=settings["base_url"],
            work_dir=work_dir,
            browser_settings=settings["browser"],
            login_settings=settings["login"],
        )

        case_text = case_text_path.read_text(encoding="utf-8")
        logging.debug("Case ID %s: fetched text length=%s", case_id, len(case_text))
        logging.debug("Case ID %s: fetched text preview=%r", case_id, case_text[:800])

        entries = build_case_json(case_text, settings["max_chars"], settings["log_filter"])
        logging.debug("Case ID %s: extracted entries=%s", case_id, len(entries))
        if not entries:
            logging.info("case_id=%s result=skipped reason=no_entries", case_id)
            return
        if entries[-1]["type"].lower() != "answer":
            if settings["llm"]["allow_partial"]:
                last_answer_index = None
                for idx in range(len(entries) - 1, -1, -1):
                    if entries[idx]["type"].lower() == "answer":
                        last_answer_index = idx
                        break
                if last_answer_index is None:
                    logging.info(
                        "case_id=%s result=skipped reason=no_answer_entry",
                        case_id,
                    )
                    return
                entries = entries[: last_answer_index + 1]
                logging.info(
                    "case_id=%s result=partial reason=last_entry_not_answer entries=%s",
                    case_id,
                    len(entries),
                )
            else:
                logging.info(
                    "case_id=%s result=skipped reason=last_entry_not_answer",
                    case_id,
                )
                return
        output_path = work_dir / f"{case_id}.json"
        output_path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )

        # プロンプトのスキーマ（type/created_on/text）に合わせる。
        llm_entries = [
            {
                "type": entry["type"].lower(),
                "created_on": entry["date"],
                "text": entry["data"],
            }
            for entry in entries
        ]
        llm_input = json.dumps(llm_entries, ensure_ascii=False, indent=2)
        logging.debug("Case ID %s: llm input=%s", case_id, llm_input)
        llm_text = call_llm(case_id, llm_input, settings["llm"])
        llm_json = parse_llm_json(llm_text)
        judgement, _reason = parse_llm_judgement(llm_text)

        decision_value = None
        if judgement:
            decision_value = judgement
        elif llm_json and isinstance(llm_json, dict):
            decision_value = str(llm_json.get("decision", "")).lower()

        webhooks = [settings["teams"]["default"]]
        if decision_value in {"却下", "reject", "rejected", "ng", "fail"}:
            webhooks.append(settings["teams"]["reject"])
        if settings["teams"]["enabled"]:
            notify_teams(case_id, llm_text, llm_json, webhooks)
        logging.info("case_id=%s result=%s", case_id, decision_value or "unknown")
    except Exception:
        logging.exception("Case ID %s: failed to process", case_id)


def monitor_directory(settings):
    # 追加依存を避けるためポーリングで監視する。
    monitor_dir = settings["monitor_dir"]
    monitor_dir.mkdir(parents=True, exist_ok=True)
    case_id_re = re.compile(rf"^(?P<case_id>\d{{{settings['case_id_digits']}}})\.txt$")
    logging.debug(
        "monitor_dir=%s process_existing=%s poll_interval=%s case_id_digits=%s",
        monitor_dir,
        settings["process_existing"],
        settings["poll_interval"],
        settings["case_id_digits"],
    )

    processed = set()
    if not settings["process_existing"]:
        for entry in monitor_dir.iterdir():
            if entry.is_file() and case_id_re.match(entry.name):
                processed.add(entry)
        logging.debug("初期既存ファイルを除外しました: %s", len(processed))

    while True:
        try:
            logging.debug("スキャン中: %s", monitor_dir)
            for path in sorted(monitor_dir.iterdir()):
                if STOP_REQUESTED:
                    logging.info("停止要求により監視を終了します。")
                    return
                if not path.is_file():
                    continue
                match = case_id_re.match(path.name)
                if not match:
                    continue
                if path in processed:
                    continue
                logging.debug("処理対象を検出: %s", path)
                if not wait_for_stable_size(path):
                    continue
                case_id = match.group("case_id")
                process_case(case_id, settings)
                processed.add(path)
                try:
                    path.unlink()
                    logging.debug("処理済みファイルを削除しました: %s", path)
                except FileNotFoundError:
                    pass
                if STOP_REQUESTED:
                    logging.info("停止要求により監視を終了します。")
                    return
        except Exception:
            logging.exception("Monitor loop error")
        time.sleep(settings["poll_interval"])


def load_settings():
    # 環境変数とデフォルト値から設定を組み立てる。
    base_dir = Path(__file__).resolve().parent
    return {
        "monitor_dir": Path(os.environ.get("MONITOR_DIR", base_dir / "monitor")),
        "work_dir": Path(os.environ.get("WORK_DIR", base_dir / "work")),
        "case_id_digits": int(os.environ.get("CASE_ID_DIGITS", "8") or "8"),
        "poll_interval": float(os.environ.get("POLL_INTERVAL", "2")),
        "process_existing": os.environ.get("PROCESS_EXISTING", "").lower()
        in {"1", "true", "yes"},
        "base_url": os.environ.get("BASE_URL", "http://localhost:8080/"),
        "max_chars": int(os.environ.get("MAX_CHARS", "6000")),
        "log_filter": {
            "enabled": os.environ.get("LOG_FILTER_ENABLED", "true").lower()
            in {"1", "true", "yes"},
            "max_line_len": int(os.environ.get("LOG_FILTER_MAX_LINE_LEN", "200")),
        },
        "browser": {
            "user_data_dir": os.environ.get("CHROME_USER_DATA_DIR"),
            "profile_dir": os.environ.get("CHROME_PROFILE_DIR"),
            "channel": os.environ.get("BROWSER_CHANNEL", "chrome"),
            "headless": os.environ.get("HEADLESS", "").lower() in {"1", "true", "yes"},
            "keep_open": os.environ.get("KEEP_BROWSER_OPEN", "").lower()
            in {"1", "true", "yes"}
            or os.environ.get("KEEP_BROWER_OPEN", "").lower() in {"1", "true", "yes"},
            "wait_seconds": int(os.environ.get("WAIT_SECONDS", "0") or "0"),
        },
        "login": {
            "url": os.environ.get("LOGIN_URL", "http://localhost:8080/login"),
            "username": os.environ.get("LOGIN_USERNAME", "testuser"),
            "password": os.environ.get("LOGIN_PASSWORD", "password"),
            "selectors": {
                "username": os.environ.get(
                    "LOGIN_USERNAME_SELECTOR", "input[name='username']"
                ),
                "password": os.environ.get(
                    "LOGIN_PASSWORD_SELECTOR", "input[name='password']"
                ),
                "submit": os.environ.get(
                    "LOGIN_SUBMIT_SELECTOR",
                    "button[type='submit'], input[type='submit']",
                ),
            },
        },
        "llm": {
            "base_url": os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
            "api_key": os.environ.get("LLM_API_KEY", ""),
            "model": os.environ.get("LLM_MODEL", "llama3.2:1b"),
            "prompt": os.environ.get("LLM_PROMPT", ""),
            "temperature": float(os.environ.get("LLM_TEMPERATURE", "0.2")),
            "timeout": int(os.environ.get("LLM_TIMEOUT", "60")),
            "cert_file": os.environ.get("LLM_CERT_FILE", ""),
            "allow_partial": os.environ.get("LLM_ALLOW_PARTIAL", "").lower()
            in {"1", "true", "yes"},
        },
        "teams": {
            "enabled": os.environ.get("TEAMS_ENABLED", "true").lower()
            in {"1", "true", "yes"},
            "default": os.environ.get("TEAMS_WEBHOOK_URL", ""),
            "reject": os.environ.get("TEAMS_REJECT_WEBHOOK_URL", ""),
        },
        "logging": {
            "enabled": os.environ.get("LOG_ENABLED", "true").lower()
            in {"1", "true", "yes"},
            "level": os.environ.get("LOG_LEVEL", "INFO").upper(),
        },
    }


def main():
    load_dotenv()
    settings = load_settings()
    if settings["logging"]["enabled"]:
        logging.basicConfig(
            level=settings["logging"]["level"],
            format="%(asctime)s %(levelname)s %(message)s",
        )
    else:
        logging.disable(logging.CRITICAL)
    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)
    monitor_directory(settings)


if __name__ == "__main__":
    main()
