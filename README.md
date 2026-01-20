# missend-detector

Case IDのページ本文（`body`のテキスト）を取得して`<caseid>.txt`として保存するスクリプトです。Chromeのログイン済みプロファイルをPlaywright経由で使えるため、追加の認証は不要です。
未ログインでログインページにリダイレクトされた場合は、指定した認証情報で自動ログインします。

## 使い方

```
python3 fetch_case_page.py 12345678
```

入力プロンプトでCase IDを指定することもできます。

```
python3 fetch_case_page.py
```

## 事前準備

必要なモジュール:

- `playwright`
- `requests` (monitor_service.py のみ)

```
python3 -m pip install playwright requests
python3 -m playwright install
```

## オプション

- `--base-url` BaseURL (default: env `BASE_URL` or `http://localhost:8080/`)
- `--output-dir` 保存先ディレクトリ (default: env `WORK_DIR` or `./work`)
- `--user-data-dir` Chromeのユーザーデータディレクトリ (default: env `CHROME_USER_DATA_DIR`)
- `--profile-dir` Chromeのプロファイル名 (default: env `CHROME_PROFILE_DIR`)
- `--channel` Playwrightのブラウザチャネル (default: env `BROWSER_CHANNEL` or `chrome`)
- `--headless` ヘッドレスで実行 (default: env `HEADLESS`)
- `--login-url` ログインページURL (default: env `LOGIN_URL` or `http://localhost:8080/login`)
- `--login-username` ログインユーザー名 (default: env `LOGIN_USERNAME` or `testuser`)
- `--login-password` ログインパスワード (default: env `LOGIN_PASSWORD` or `password`)

## 例

```
BASE_URL=http://localhost:8080/ WORK_DIR=./out \
python3 fetch_case_page.py 12345678
```

Chromeのログイン状態を使う場合は`--user-data-dir`と`--profile-dir`を指定してください。

```  
python3 fetch_case_page.py 12345678 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome" \
  --profile-dir="Default"
```

### OS別のChromeプロファイルパス例

macOS:

```
$HOME/Library/Application Support/Google/Chrome
```

Windows:

```
C:\Users\<User>\AppData\Local\Google\Chrome\User Data
```

### ログインフォームのセレクタ調整

フォームの入力要素がデフォルトと異なる場合は、環境変数で上書きできます。

- `LOGIN_USERNAME_SELECTOR` (default: `input[name='username']`)
- `LOGIN_PASSWORD_SELECTOR` (default: `input[name='password']`)
- `LOGIN_SUBMIT_SELECTOR` (default: `button[type='submit'], input[type='submit']`)

## 抽出スクリプト

`<caseid>.txt`からQuestion/Answerを抽出し、`./work/<caseid>.json`に保存します。

```
python3 extract_case_entries.py ./work/00000000.txt
```

出力先は環境変数`WORK_DIR`で変更できます。

## 常駐サービス

監視ディレクトリに`<caseid>.txt`が作成されると、Case取得 → 抽出 → LLM判定 → Teams通知までを行います。

```
python3 monitor_service.py
```

### .env の利用

リポジトリ直下の`.env`を読み込むため、環境変数は`.env`で設定可能です。`.env.example`を参考にしてください。

### 主要な環境変数

- `MONITOR_DIR` (default: `./monitor`)
- `WORK_DIR` (default: `./work`)
- `POLL_INTERVAL` (default: `2`)
- `PROCESS_EXISTING` (default: `false`)
- `MAX_CHARS` (default: `6000`)
- `BASE_URL` (default: `http://localhost:8080/`)
- `LOGIN_URL` (default: `http://localhost:8080/login`)
- `LOGIN_USERNAME` (default: `testuser`)
- `LOGIN_PASSWORD` (default: `password`)
- `LOGIN_USERNAME_SELECTOR` (default: `input[name='username']`)
- `LOGIN_PASSWORD_SELECTOR` (default: `input[name='password']`)
- `LOGIN_SUBMIT_SELECTOR` (default: `button[type='submit'], input[type='submit']`)
- `CHROME_USER_DATA_DIR`
- `CHROME_PROFILE_DIR`
- `BROWSER_CHANNEL` (default: `chrome`)
- `HEADLESS` (default: `false`)
- `LLM_BASE_URL` (default: `http://localhost:11434/v1`)
- `LLM_API_KEY`
- `LLM_MODEL` (default: `llama3.2:1b`)
- `LLM_PROMPT`
- `LLM_TEMPERATURE` (default: `0.2`)
- `LLM_TIMEOUT` (default: `60`)
- `TEAMS_WEBHOOK_URL`
- `TEAMS_REJECT_WEBHOOK_URL`

### LLMの戻り値について

LLMの出力がJSONで`decision`キーを含む場合、`reject`/`ng`などの値なら却下通知URLにも送信します。
また、LLMの出力が「査閲結果：承認/却下/不明」の形式であれば、その結果を優先して通知します。
Teams通知はAdaptive Card形式で送信します。

### LLMプロンプト

`LLM_PROMPT`未指定時は以下のプロンプトを使用し、`{entries}`に抽出済みの履歴JSONを埋め込んで送信します。
`LLM_PROMPT`に`{entries}`が含まれない場合は警告を出力します。

```
あなたはサポートチケットの内容整合性を確認するAIです。

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
```

## テスト方法

`dummyWeb`のコンテンツを使って動作確認できます。

```
cd dummyWeb
docker compose up -d
```

```
python3 fetch_case_page.py 00000000
```

終了後に`dummyWeb/00000000.html`のHTMLが`00000000.txt`として保存されていればOKです。

## Chromeの起動手順

ログイン済みのプロファイルを使う場合は、既存のChromeをすべて終了してから実行してください。

1. Chromeを完全に終了する
2. `--user-data-dir`にプロファイルの親ディレクトリを指定
3. `--profile-dir`にプロファイル名（例: `Default` や `Profile 1`）を指定して実行

Chromeを開いたままにしたい場合は、別のプロファイルを指定するか、`--user-data-dir`で複製したプロファイルを使ってください。
