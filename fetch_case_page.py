#!/usr/bin/env python3
import argparse
import logging
import os
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from env_loader import load_dotenv


def validate_case_id(case_id):
    return bool(re.fullmatch(r"\d{8}", case_id))


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


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Case IDのページを取得し、HTMLを<caseid>.txtとして保存します。"
    )
    parser.add_argument(
        "case_id",
        nargs="?",
        help="8桁のCase ID。未指定の場合は入力を促します。",
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
    args = parser.parse_args()

    if args.log_enabled:
        logging.basicConfig(
            level=args.log_level,
            format="%(asctime)s %(levelname)s %(message)s",
        )
    else:
        logging.disable(logging.CRITICAL)

    case_id = args.case_id or input("8桁のCase IDを入力してください: ").strip()
    if not validate_case_id(case_id):
        raise SystemExit("Case IDは8桁の数字で指定してください。")

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
            if args.wait_seconds > 0:
                logging.info("ページ表示後に%s秒待機します。", args.wait_seconds)
                page.wait_for_timeout(args.wait_seconds * 1000)
            page_source = page.inner_text("body")
            logging.debug("取得した本文文字数=%s", len(page_source))
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
