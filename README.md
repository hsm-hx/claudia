# Claude Code Usage Monitor

`~/.claude/projects/` のローカルデータを読み取り、Discord に使用量を通知するモニターです。
**Anthropic API クレジット不要。** Claude Code Pro/Max サブスクリプションで動作します。

## 通知内容

### monitor.py（毎時0分）

4時間ウィンドウの使用率をリアルタイムで通知します。

```
Claude Code 使用量レポート
4時間ウィンドウ  10:00 〜 14:00 JST
████░░░░░░ 42.5% 使用
📊 使用済み: 212.5K / 500K tokens
💚 残り:     287.5K tokens  (57.5%)
   📥 Input 150.0K  📤 Output 30.0K  ⚡ Cache 32.5K

過去24時間          過去7日間
450.0K tokens       2.10M tokens
```

### アラート条件

| 条件 | 内容 |
|------|------|
| 毎時0分 | 定期レポート（常時） |
| ウィンドウ切り替わり | 🔄 リセット通知 + 前ウィンドウの使用量 |
| 残り25%以下 | 🚨 アラート（ウィンドウ内で1回のみ） |

### usage_reporter.py（日次・週次）

| スケジュール | 内容 |
|------------|------|
| 毎日 09:00 JST | 昨日の使用量レポート |
| 毎週月曜 09:00 JST | 先週の使用量レポート |

## セットアップ

### 1. 環境変数

```bash
cp .env.example .env
# .env を編集
```

| 変数 | 必須 | デフォルト | 説明 |
|------|------|-----------|------|
| `DISCORD_WEBHOOK_URL` | ✅ | — | サーバー設定 → 連携サービス → Webhook |
| `MAX_TOKENS_4H` | | `500000` | 4時間ウィンドウの上限トークン数（実際の上限に合わせて調整） |
| `LOW_THRESHOLD_PCT` | | `25` | 残り何%以下でアラートを送るか |

> `MAX_TOKENS_4H` は Anthropic が公開していないため、実際に使っていて「制限に当たった」前後のトークン数を見ながら調整してください。

### 2. 動作確認

```bash
# 今すぐ通知を送る（テスト）
python3 monitor.py

# 日次レポートのテスト
python3 usage_reporter.py --daily --dry-run
```

### 3. cron 登録

```bash
chmod +x setup_cron.sh
./setup_cron.sh
```

登録されるジョブ：

```
0 * * * *   monitor.py          ← 毎時0分（4時間ウィンドウ使用率）
0 0 * * *   usage_reporter.py --daily   ← 毎日 09:00 JST
0 0 * * 1   usage_reporter.py --weekly  ← 毎週月曜 09:00 JST
```

## 仕組み

```
cron（毎時0分）
  └── monitor.py
        ├── ~/.claude/projects/**/*.jsonl を読み取り
        ├── 現在の4時間ウィンドウ（0-4, 4-8, ... 20-24 UTC）でフィルタ
        ├── トークン集計（4h / 24h / 7d）
        ├── ~/.claude/usage_monitor_state.json で状態管理
        │     ├── ウィンドウ切り替わり検知
        │     └── 低残量アラート送信済みフラグ
        └── Discord Webhook → Embed 送信
```

## 注意事項

- `MAX_TOKENS_4H` は Anthropic の公式な上限値ではなく、自分で設定する目安値です。
- 状態ファイルは `~/.claude/usage_monitor_state.json` に保存されます。
- ログは `logs/monitor.log`, `logs/daily.log`, `logs/weekly.log` に出力されます。
