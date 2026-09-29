# anemone-site-monitor

anemone.blue / anemone2.blue の全ページ(サイトマップ掲載分)を毎時、iPhone・Android・PC の実ブラウザと同じヘッダーで取得し、
空ページ・HTML以外・途中切れ・HTTPエラーを検知したら Slack に通知する。

- 新規異常と復旧のたびに通知し、異常が続く間は6時間ごとに再通知する
- Slack の Webhook は Secret `SLACK_WEBHOOK_URL` に入れる
- ローカル確認: `pip install -r requirements.txt && python check_sites.py --dry-run`
