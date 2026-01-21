#!/usr/bin/env python3
import argparse
import logging
import os
import re
from pathlib import Path
from logging.handlers import RotatingFileHandler
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from env_loader import load_dotenv


def validate_case_id(case_id, digits):
    return bool(re.fullmatch(rf"\d{{{digits}}}", case_id))


def build_url(base_url, case_id):
    if "?" in base_url or base_url.endswith("="):
        return f"{base_url}{case_id}"
    base = base_url if base_url.endswith("/") else base_url + "/"
    return urljoin(base, case_id)


def normalize_url(url):
    return url.rstrip("/")


def login_if_needed(page, login_url, username, password, selectors):
    if normalize_url(page.url).startswith(normalize_url(login_url)):
        logging.info("ログイン画面を検出しました。ログインを試みます。")
        page.fill(selectors["username"], username)
        page.fill(selectors["password"], password)
        page.click(selectors["submit"])
        try:
            page.wait_for_url(
                lambda url: not normalize_url(url).startswith(normalize_url(login_url)),
                timeout=30000,
            )
        except Exception:
            logging.debug("ログイン後のURL遷移を検出できませんでした。")
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


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Case IDのページを取得し、HTMLを<caseid>.txtとして保存します。"
    )
    parser.add_argument(
        "case_id",
        nargs="?",
        help="Case ID。未指定の場合は入力を促します。",
    )
    parser.add_argument(
        "--case-id-digits",
        type=int,
        default=int(os.environ.get("CASE_ID_DIGITS", "8") or "8"),
        help="Case IDの桁数 (default: env CASE_ID_DIGITS or 8)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BASE_URL", "http://localhost:8080/"),
        help="BaseURL (default: env BASE_URL or http://localhost:8080/)",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("WORK_DIR", os.environ.get("OUTPUT_DIR")),
        help="保存先ディレクトリ (default: env WORK_DIR or ./work)",
    )
    parser.add_argument(
        "--user-data-dir",
        default=os.environ.get("CHROME_USER_DATA_DIR"),
        help="Chromeのユーザーデータディレクトリ (default: env CHROME_USER_DATA_DIR)",
    )
    parser.add_argument(
        "--profile-dir",
        default=os.environ.get("CHROME_PROFILE_DIR"),
        help="Chromeのプロファイル名 (default: env CHROME_PROFILE_DIR)",
    )
    parser.add_argument(
        "--channel",
        default=os.environ.get("BROWSER_CHANNEL", "chrome"),
        help="Playwrightで使用するブラウザチャネル (default: env BROWSER_CHANNEL or chrome)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=os.environ.get("HEADLESS", "").lower() in {"1", "true", "yes"},
        help="ヘッドレスで実行 (default: env HEADLESS)",
    )
    parser.add_argument(
        "--login-url",
        default=os.environ.get("LOGIN_URL", "http://localhost:8080/login"),
        help="ログインページURL (default: env LOGIN_URL or http://localhost:8080/login)",
    )
    parser.add_argument(
        "--login-username",
        default=os.environ.get("LOGIN_USERNAME", "testuser"),
        help="ログインユーザー名 (default: env LOGIN_USERNAME or testuser)",
    )
    parser.add_argument(
        "--login-password",
        default=os.environ.get("LOGIN_PASSWORD", "password"),
        help="ログインパスワード (default: env LOGIN_PASSWORD or password)",
    )
    parser.add_argument(
        "--log-enabled",
        action="store_true",
        default=os.environ.get("LOG_ENABLED", "true").lower() in {"1", "true", "yes"},
        help="ログ出力を有効にします (default: env LOG_ENABLED)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO").upper(),
        help="ログレベル (default: env LOG_LEVEL or INFO)",
    )
    parser.add_argument(
        "--log-dir",
        default=os.environ.get("LOG_DIR", ""),
        help="ログディレクトリ (default: env LOG_DIR or none)",
    )
    parser.add_argument(
        "--log-max-bytes",
        type=int,
        default=int(os.environ.get("LOG_MAX_BYTES", "1048576") or "1048576"),
        help="ログローテーションサイズ (bytes) (default: env LOG_MAX_BYTES or 1048576)",
    )
    parser.add_argument(
        "--log-backup-count",
        type=int,
        default=int(os.environ.get("LOG_BACKUP_COUNT", "3") or "3"),
        help="ログローテーション世代数 (default: env LOG_BACKUP_COUNT or 3)",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        default=os.environ.get("KEEP_BROWSER_OPEN", "").lower() in {"1", "true", "yes"},
        help="ブラウザを閉じる前に待機します (default: env KEEP_BROWSER_OPEN)",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=int(os.environ.get("WAIT_SECONDS", "0") or "0"),
        help="ページ表示後に待機する秒数 (default: env WAIT_SECONDS or 0)",
    )
    parser.add_argument(
        "--save-screenshot",
        action="store_true",
        default=os.environ.get("SAVE_SCREENSHOT", "").lower() in {"1", "true", "yes"},
        help="スクリーンショットを保存します (default: env SAVE_SCREENSHOT)",
    )
    args = parser.parse_args()

    if args.log_enabled:
        handlers = [logging.StreamHandler()]
        if args.log_dir:
            log_dir = Path(args.log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "fetch_case_page.log"
            handlers.append(
                RotatingFileHandler(
                    log_path,
                    maxBytes=args.log_max_bytes,
                    backupCount=args.log_backup_count,
                    encoding="utf-8",
                )
            )
        logging.basicConfig(
            level=args.log_level,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=handlers,
        )
    else:
        logging.disable(logging.CRITICAL)

    prompt = f"{args.case_id_digits}桁のCase IDを入力してください: "
    case_id = args.case_id or input(prompt).strip()
    if not validate_case_id(case_id, args.case_id_digits):
        raise SystemExit(f"Case IDは{args.case_id_digits}桁の数字で指定してください。")

    base_url = args.base_url
    default_work_dir = Path(__file__).resolve().parent / "work"
    output_root = args.output_dir or str(default_work_dir)
    output_dir = Path(output_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    url = build_url(base_url, case_id)
    logging.debug("case_id=%s url=%s output_dir=%s", case_id, url, output_dir)
    selectors = {
        "username": os.environ.get("LOGIN_USERNAME_SELECTOR", "input[name='username']"),
        "password": os.environ.get("LOGIN_PASSWORD_SELECTOR", "input[name='password']"),
        "submit": os.environ.get(
            "LOGIN_SUBMIT_SELECTOR", "button[type='submit'], input[type='submit']"
        ),
    }

    launch_args = []
    if args.profile_dir:
        launch_args.append(f"--profile-directory={args.profile_dir}")

    with sync_playwright() as p:
        if args.user_data_dir:
            logging.debug("Launching persistent context: %s", args.user_data_dir)
            context = p.chromium.launch_persistent_context(
                user_data_dir=args.user_data_dir,
                channel=args.channel,
                headless=args.headless,
                args=launch_args,
            )
            page = context.pages[0] if context.pages else context.new_page()
        else:
            logging.debug("Launching browser (non-persistent)")
            browser = p.chromium.launch(
                channel=args.channel,
                headless=args.headless,
                args=launch_args,
            )
            context = browser.new_context()
            page = context.new_page()

        try:
            logging.info("ページへアクセスします: %s", url)
            page.goto(url, wait_until="load", timeout=30000)
            login_if_needed(
                page,
                login_url=args.login_url,
                username=args.login_username,
                password=args.login_password,
                selectors=selectors,
            )
            if normalize_url(page.url).startswith(normalize_url(args.login_url)):
                logging.info("ログインページに留まっているため再アクセスします: %s", url)
                page.goto(url, wait_until="load", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                logging.debug("networkidle待機がタイムアウトしました。")
            if args.wait_seconds > 0:
                logging.info("ページ表示後に%s秒待機します。", args.wait_seconds)
                page.wait_for_timeout(args.wait_seconds * 1000)
            page_html = collect_page_content(page)
            page_text = ""
            if not page_html.strip():
                logging.info("HTMLが空のためテキストを取得します。")
                page_text = collect_visible_text(page)
            page_source = page_html or page_text
            logging.debug(
                "取得したHTML文字数=%s テキスト文字数=%s",
                len(page_html),
                len(page_text),
            )
            if args.save_screenshot:
                screenshot_path = output_dir / f"{case_id}.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                logging.info("スクリーンショットを保存しました: %s", screenshot_path)
        finally:
            if args.keep_open:
                logging.info("ブラウザを閉じる前に待機します。Enterで終了します。")
                try:
                    input()
                except EOFError:
                    pass
            context.close()

    output_path = output_dir / f"{case_id}.txt"
    output_path.write_text(page_source, encoding="utf-8")
    logging.info("保存しました: %s", output_path)
    print(f"保存しました: {output_path}")


if __name__ == "__main__":
    main()
