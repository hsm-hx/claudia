# Claude Code Usage Monitor

Anthropic API のレートリミット消費状況をリアルタイムで監視し、Discord に通知する Cloud Run サービスです。

## 通知内容

毎時0分に現在のレートリミット ウィンドウの使用状況を送信します。

```
Claude Code レートリミット使用状況
チェック時刻 (JST): 2026-03-12 10:00   リセットまで: 2時間14分後

トークン使用率（ウィンドウ上限: 200,000）
████░░░░░░ 42.5% 使用
✅ 使用済み:  85,000 トークン
💚 残り:     115,000 トークン  (57.5%)

リクエスト数（上限: 1,000）
██░░░░░░░░ 23.1% 使用  残り: 769 req
```

### アラート条件

| 条件 | 内容 |
|------|------|
| 毎時0分 | 定期レポート（常時） |
| 残り25%以下 | 🚨 アラート（ウィンドウ内で1回のみ） |
| レートリミット リセット検知 | 🔄 通知（新ウィンドウ開始） |

## カバー範囲

| 使用形態 | 対象 |
|---------|------|
| Claude GitHub Actions（API キー経由） | ✅ |
| Claude Code CLI（API キー経由） | ✅ |
| claude.ai Web UI / Pro / Max サブスクリプション | ❌（API が未公開） |

> **注**: レートリミット情報は `POST /v1/messages/count_tokens` のレスポンスヘッダーから取得します（トークン消費ゼロ）。

## 仕組み

```
Cloud Run Service (min-instances=1, always-on)
  └── APScheduler: CronTrigger(minute=0)  ─ 毎時0分
        ├── POST /v1/messages/count_tokens  ← プローブ（無料）
        │     └── レスポンスヘッダー:
        │           anthropic-ratelimit-tokens-limit
        │           anthropic-ratelimit-tokens-remaining
        │           anthropic-ratelimit-tokens-reset
        ├── リセット検知（reset タイムスタンプの変化を追跡）
        ├── 残り25%以下チェック
        └── Discord Webhook → Embed 送信
```

## セットアップ

### 1. 環境変数

```bash
cp .env.example .env
# .env を編集
```

| 変数 | 必須 | 説明 |
|------|------|------|
| `ANTHROPIC_API_KEY` | ✅ | Anthropic コンソール → API Keys |
| `DISCORD_WEBHOOK_URL` | ✅ | サーバー設定 → 連携サービス → Webhook |

### 2. ローカルで動作確認

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:8080/        ヘルスチェック（現在の使用率を返す）
# → POST http://localhost:8080/check  手動チェック実行
```

### 3. Cloud Run にデプロイ

```bash
export GCP_PROJECT=your-project-id
export GCP_REGION=asia-northeast1
export ANTHROPIC_API_KEY=sk-ant-...
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

chmod +x deploy.sh
./deploy.sh
```

### 4. 手動チェックのトリガー

```bash
SERVICE_URL=$(gcloud run services describe claude-usage-monitor \
  --region=asia-northeast1 --format='value(status.url)')

curl -X POST "${SERVICE_URL}/check" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)"
```

## 注意事項

- Cloud Run インスタンスが再起動するとインメモリの状態（通知フラグ等）がリセットされます。
- レートリミットのウィンドウ長は Anthropic のプランによって異なります（4〜8時間など）。ヘッダーの `reset` タイムスタンプで正確な残り時間を確認できます。
