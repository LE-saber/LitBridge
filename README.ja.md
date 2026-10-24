# LitBridge

[English](https://github.com/LE-saber/LitBridge/blob/main/README.md) | [简体中文](https://github.com/LE-saber/LitBridge/blob/main/README.zh-CN.md) | [繁體中文](https://github.com/LE-saber/LitBridge/blob/main/README.zh-TW.md) | **日本語**

**AI エージェント向けの、ローカル処理を基本とする文献ワークフローと文書正規化サービス。**

LitBridge は検索結果、出典、原本を保持し、再開可能な取得ジョブを管理します。文書を元のページやノードを参照できる Markdown に変換し、CLI または MCP から読めるようにします。ネットワーク上の文献ソースはすべて公開 Provider プロトコルを通じて接続します。コアには文献サイトの実装、サイト一覧、ログイン手順、ソース固有の認証情報を同梱しません。

| プロジェクト情報 | 現在の基準 |
| --- | --- |
| アプリケーション / 汎用クライアント | 0.2.1 |
| Provider プロトコル | 1.0 |
| 実行環境 | Python 3.11 以降 |
| インターフェース | CLI と stdio を使う 16 個の MCP ツール |
| 初期状態のソース / クラウド処理 | Provider は未有効化、クラウド処理は無効 |
| 配布 | このリポジトリの Releases またはソースからのインストール |

## 機能

- 能力宣言と明示的な有効化を備えた Provider プロトコル 1.0。複数ソースの結果統合、DOI／ソース識別、キャッシュ、タイムアウトによる障害分離、サーキットブレーカーを提供します。
- 永続的なジョブキュー、リース、失敗分類、人の操作による復旧、既存原本の再利用。取得、形式検証、正規化、読み取りの結果を個別に報告します。
- サイズ制限付きの PDF/XML 原本保存と SHA-256 検証。元のページ／ノード参照付きの Markdown と構造化ブロックを生成し、明示的に取り込んだローカル HTML も処理できます。
- 任意のオフラインレイアウト解析／OCR、ページごとのチェックポイント、中断後の再開。クラウド数式認識と設定可能な画像モデル比較も任意で、クラウド処理は初期状態で無効です。
- CLI と 16 個の MCP ツールがサービス層を共有します。汎用ブラウザーツールは Provider が渡す制御された対象だけを操作し、サイト固有のルールは持ちません。

## インストール

Python 3.11 以降が必要です。[Releases](https://github.com/LE-saber/LitBridge/releases) から wheel を入手するか、ソースディレクトリからインストールします。以下は Windows PowerShell の例です。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[mcp]"
Copy-Item examples/litbridge.toml litbridge.local.toml
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml providers
```

Provider をインストールして有効化するまでは、一覧は空です。公開プロトコルを試すための中立的なローカルカタログの例を使う場合：

```powershell
.venv/Scripts/python.exe -m pip install --no-deps -e examples/localcatalog
```

自分が利用できるメタデータを JSON 配列として保存します。例：`[{"title":"Synthetic study","doi":"10.5555/example","year":2024}]`。その後、ローカル設定に次を追加します。

```toml
enabled_plugins = ["localcatalog"]
[plugin_options.localcatalog]
path = "D:/YOUR_PATH/catalog.json"
```

```powershell
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml search "Synthetic" --provider localcatalog
```

ローカルカタログの例が提供するのはメタデータだけです。例の DOI がオンラインに存在する保証はなく、本文は提供しません。他のソースは別途インストールし、ID を `enabled_plugins` に明示的に追加してください。コアはそれらのリポジトリや実装に依存しません。

## 使い方と MCP

```powershell
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml doctor
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml mcp
```

MCP は stdio を使用します。[設定例](examples/mcp.json) を参照してください。ツールは `providers`、`search`、`resolve`、`access`、`retrieve`、`references`、`import_url`、`doctor`、`batch`、`job_create`、`job_run`、`job_status`、`job_history`、`human_run`、`normalize`、`read` です。まずソースの能力を確認して少数の対象を選び、取得した原本を保持してから正規化と読み取りの結果を確認します。詳しくは [読書ワークフロー](docs/READING_WORKFLOW.md) を参照してください。

Release の `litbridge-core-plugin.zip` は、コアサービスを含む汎用 Codex/MCP クライアントラッパーです。文献サイトの実装は含みません。展開後に同梱の手順で依存関係を導入し、ローカル設定を指定します。wheel は既存の Python 環境向け、ソース ZIP は開発向けです。このバージョンは PyPI に公開されていないため、`pip install litbridge` で取得できるとは限りません。

## 正規化とモデル

軽量パーサーは大型モデルを必要としません。複雑なレイアウトには別のオフライン実行環境を利用できます。スキャン画像、2 段組の読み順、表、数式は確認が必要な場合があります。`ready` や処理進捗は精度を表しません。開くためのパスワードが不明な文書はスキップし、原本は保持します。

画像モデルは明示的なローカル設定で接続し、認証情報はプロセス環境または追跡対象外のファイルに置きます。通常は HTTPS が必須です。外部処理は明示的に有効化された選択済み数式の切り抜き画像に限定されます。モデル同士の一致率は正解率ではありません。[モデル設定](docs/MODEL_SERVICES.md) と [正規化](docs/NORMALIZATION.md) を参照してください。

## セキュリティと境界

文献、Web ページ、モデル出力は信頼できないデータとして扱います。Provider はユーザーが信頼する Python パッケージであり、プロセスサンドボックスで隔離されていません。コアは購読権限を付与せず、CAPTCHA の解決や代理ログインも行いません。API の権限、ブラウザーの権限、取得成功、可読性はそれぞれ確認が必要です。コアのネットワーク処理は HTTPS、対象ドメイン、サイズ、リダイレクト時の認証情報の扱いを制限します。

キー、機関のセッション、ブラウザープロファイル、ダウンロードした論文、全文、モデル重み、ローカルレポートをコミットしないでください。クラウド処理は選択した画像を送信し、料金が発生する場合があります。設定と上限を確認してください。[セキュリティとプライバシー](docs/SECURITY.md) に詳細があります。

## 開発

```powershell
.venv/Scripts/python.exe -m pip install -e ".[dev,mcp,browser]"
.venv/Scripts/python.exe -m pytest -m "not browser and not live" -q
.venv/Scripts/python.exe scripts/update_manifest.py
.venv/Scripts/python.exe scripts/check_manifest.py
.venv/Scripts/python.exe scripts/build_plugin.py
```

公開 API：[Provider プロトコル](docs/PROVIDER_PROTOCOL.md)。バージョン：[変更履歴](CHANGELOG.md)。CI は機関セッションにアクセスせず、独立した環境でコア、公式 MCP SDK、配布パッケージを検証します。合成テストは実際のネットワーク／ブラウザー受け入れ検証を代替しません。

現在のバージョンは 0.2.1、Provider プロトコルは 1.0 です。以前の設定のソース固有項目は、対応する独立 Provider のオプションに移します。コアはソースがない状態で起動します。

## ライセンス

このリポジトリのプロジェクトコードと文書は [MIT ライセンス](LICENSE) です。コア、公開 Provider プロトコル、CLI/MCP、汎用クライアント、正規化／モデルインターフェース、例、テストを含みます。独立した Provider パッケージには各自のライセンスが適用され、このリポジトリのライセンスはそれらの権利を付与しません。第三者の依存関係とモデル重みは各自のライセンスに従い、原本文献の著作権は権利者に帰属します。
