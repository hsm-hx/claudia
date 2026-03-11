# Claude Code Usage Monitor

Anthropic API の使用量を監視し、Discord に通知する Cloud Run サービスです。

## 通知タイミング

| タイミング | 内容 |
|-----------|------|
| 毎時0分 | 当月累計コスト・トークン内訳の定期レポート |
| 請求期間リセット時 | 使用量が大幅に減少した際にリセット検知・通知 |
| 残り予算が25%以下 | 一度だけアラートを送信（次のリセットまで再送しない） |

## カバー範囲

| 使用形態 | 対象 |
|---------|------|
| Claude GitHub Actions（API キー経由） | ✅ |
| Claude Code CLI（API キー経由） | ✅ |
| claude.ai サブスクリプション（Pro/Max） | ❌（Anthropic がAPIを未公開） |

> Claude.ai の Web UI 使用量はAnthropicが公開APIを提供していないため集計対象外です。

## セットアップ

### 1. 環境変数を設定

```bash
cp .env.example .env
# .env を編集して各値を設定
```

必須変数:

| 変数 | 説明 |
|------|------|
| `ANTHROPIC_API_KEY` | Anthropic コンソール → API Keys |
| `DISCORD_WEBHOOK_URL` | サーバー設定 → 連携サービス → Webhook |

任意変数:

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MONTHLY_BUDGET_USD` | `100.0` | 月次予算（USD）。残り25%のアラート判定に使用 |
| `BILLING_CYCLE_DAY` | `1` | 請求サイクルのリセット日（1〜28日） |
| `ANTHROPIC_USAGE_PATH` | `/v1/usage` | 使用量APIのパス（組織プランは変更が必要な場合あり） |

### 2. ローカルで動作確認

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:8080/  でヘルスチェック
# → POST http://localhost:8080/check  で手動チェック実行
```

### 3. Cloud Run にデプロイ

```bash
export GCP_PROJECT=your-project-id
export GCP_REGION=asia-northeast1
export ANTHROPIC_API_KEY=sk-ant-...
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
export MONTHLY_BUDGET_USD=100.0

chmod +x deploy.sh
./deploy.sh
```

`deploy.sh` は Cloud Build でイメージをビルドし、Cloud Run にデプロイします。
`--min-instances=1` でインスタンスを常時稼働させ、APScheduler によるスケジューリングを維持します。

### 手動チェックのトリガー

```bash
SERVICE_URL=$(gcloud run services describe claude-usage-monitor \
  --region=asia-northeast1 --format='value(status.url)')

curl -X POST "${SERVICE_URL}/check" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)"
```

## アーキテクチャ

```
Cloud Run Service (min-instances=1, always-on)
  ├── FastAPI  (GET /  → ヘルスチェック、POST /check → 手動実行)
  └── APScheduler
        └── CronTrigger(minute=0)  毎時0分
              ├── Anthropic Usage API → 当月使用量を取得
              ├── リセット検知（前回比50%以上減少）
              ├── 残り予算25%以下チェック
              └── Discord Webhook → 通知送信
```

## Discord 通知の例

- **定期レポート**: 予算バー（`████░░░░░░ 42.5%`）・コスト・トークン内訳
- **リセット通知**: 🔄 紫色エンベッド
- **予算アラート**: 🚨 赤色エンベッド（閾値を超えたとき一度だけ）

## 注意事項

- Cloud Run のインスタンスが再起動するとインメモリの状態（通知フラグ）がリセットされます。
  永続化が必要な場合は Cloud Firestore または Cloud Storage を追加してください。
- `ANTHROPIC_USAGE_PATH` の正確なエンドポイントはプランによって異なる可能性があります。
  404 が返る場合は Anthropic コンソールを確認し、正しいパスを設定してください。
