# Repository Guidelines

## プロジェクト構成とモジュール整理
- `src/app.py` がFlaskアプリのエントリポイントとルート定義です。
- `templates/` にJinja2のHTMLテンプレートを置きます（例: `templates/index.html`）。
- `static/` はCSS・画像・ビルド済みJavaScriptなどのフロントエンド資産用です（例: `static/css/`, `static/js/`）。
- `requirements.txt` にPython依存関係を記載します（現在は空。Flaskなどを追加してください）。

## ビルド・テスト・開発コマンド
- 仮想環境作成: `python -m venv .venv`
- 有効化: `source .venv/bin/activate`
- 依存関係インストール: `pip install -r requirements.txt`
- ローカル起動: `python src/app.py`（`debug=True`で起動）
- Flask CLIを使う場合: `FLASK_APP=src/app.py` を設定して `flask run`

## コーディングスタイルと命名規則
- Python: PEP 8に準拠、4スペースインデント、短く明確な関数名。
- HTML/CSS: クラス名はkebab-case（例: `.hero-panel`）、ファイル名は役割に合わせる（`style.css`, `app.js`）。
- テンプレート変数: Jinja2では説明的なsnake_case（例: `{{ focus_window }}`）。
- 大きな1枚テンプレートより、小さく目的別のテンプレートを優先。

## テスト方針
- まだテスト基盤は未設定です。追加する場合は `pytest` を推奨し、`tests/` 配下に `test_*.py` で配置します。
- PRではテスト追加/省略の理由を短く記載してください。

## コミット・PRガイド
- まだ履歴がないため慣例は未定です。簡潔な命令形メッセージを使用します（例: `Add session form UI`）。
- PRには概要、背景（短く）、UI変更時はスクリーンショットを含めます。
- 関連するIssueやタスクがあればリンクします。

## 設定とアセット管理
- 秘密情報はリポジトリに入れず、環境変数で管理します。
- ビルド済みフロントエンド成果物は `static/` に置き、ソースは分離（例: `assets/`）して混在を避けます。
