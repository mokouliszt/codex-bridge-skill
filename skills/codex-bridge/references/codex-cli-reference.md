# Codex CLI リファレンス(codex-bridge用・検証済み事実と自己調査手順)

最終検証: **2026-07-10 / codex-cli 0.144.1 / ChatGPT認証(サブスク枠)**。
このドキュメントは「検証済みの事実」と「将来ズレた時の調べ方」を分離してある。
**バージョンが違ったら、まず事実を疑い、自己調査手順で再確認すること。**

---

## 1. 検証済みの事実 (0.144.1)

### 認証
- `$CODEX_HOME/auth.json` にChatGPT OAuthトークンを置くだけで `Logged in using ChatGPT`
- 構造: `{auth_mode, OPENAI_API_KEY(null), tokens{id_token, access_token, refresh_token, account_id}, last_refresh}`
- アクセストークンのリフレッシュはCLIが自動処理(こちらで実装不要)
- サブスク認証時のAPI先: `chatgpt.com/backend-api/codex/responses`。
  未認証時は `api.openai.com/v1/responses` に行き401になる(=APIキー経路。使用禁止)
- **APIキー禁止の担保**: 全スクリプトが `OPENAI_API_KEY`/`CODEX_API_KEY` をunset、
  auth.json内も null

### モデル(ChatGPT Plus契約・2026-07時点のスナップショット)
| id | default | efforts |
|---|---|---|
| gpt-5.6-sol | ✔ | low/medium/high/**xhigh/max/ultra** |
| gpt-5.6-terra | | low〜ultra |
| gpt-5.6-luna | | low〜max |
| gpt-5.5 / gpt-5.4 / gpt-5.4-mini | | low〜xhigh |

- `ultra` = "Maximum reasoning with automatic task delegation"(サブエージェント自動委譲)
- service tier `priority`("Fast", 1.5x speed, **increased usage**)が存在するが、
  クォータ消費が増えるため既定では使わない
- スナップショットは陳腐化する。**現在の一覧は必ず `models.sh list` で取ること**

### exec の主要フラグ(動作確認済み)
```
codex exec [flags] "prompt"
  --skip-git-repo-check                      gitリポ外での実行許可
  --dangerously-bypass-approvals-and-sandbox 承認スキップ+非サンドボックス実行
                                             (公式説明:「外部からサンドボックス化された環境専用」)
  -m <model>                                 モデル指定
  -c model_reasoning_effort=<effort>         xhigh/max/ultra等(config上書き)
  -c tools.web_search=true|false             web検索ツール(configで既定true化済み)
  -o <file>                                  最終メッセージをファイルへ
  --json                                     JSONLイベント出力
  -s read-only|workspace-write|danger-full-access  Codex側サンドボックスポリシー
codex exec resume --last "prompt"            直前セッションの継続(コンテキスト維持)
```
- 実行ヘッダに `model / approval / sandbox / reasoning effort` が表示されるので設定確認に使える
- 実測: web検索付きの簡単な質問で約6.4kトークン消費

### config.toml(`$CODEX_HOME/config.toml`、キー有効性確認済み)
```toml
model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"
check_updates = false

[tools]
web_search = true
```

### サンドボックス関連
- コンテナにbubblewrapが無くても**CLI同梱のbubblewrapで動く**(警告は出るが無害)
- `codex sandbox <cmd>` でCodex自前の隔離環境をネスト展開できる(動作確認済み)
- Claudeサンドボックス内なので、Codex自体は bypass(最緩)で走らせ、
  危険な実験だけCodexに `codex sandbox` を使わせる、という二層構えが可能

### feature flags(`codex features list`)
stableで有効: apps / browser_use / computer_use / image_generation / hooks /
unified_exec / fast_mode / goals / guardian_approval / tool_suggest 等。
experimental: memories。under development: enable_fanout, artifact 等。

### app-server JSON-RPC(モデル一覧の動的取得に使用)
stdioでnewline-delimited JSON-RPC:
```
-> {"jsonrpc":"2.0","id":0,"method":"initialize","params":{"clientInfo":{"name":"x","title":"x","version":"1"}}}
<- {"id":0,"result":{...}}
-> {"jsonrpc":"2.0","method":"initialized"}
-> {"jsonrpc":"2.0","id":1,"method":"model/list","params":{}}
<- {"id":1,"result":{"data":[{id, isDefault, supportedReasoningEfforts[], hidden, ...}]}}
```
実装は `scripts/models.sh`。他に `thread/*` 等のメソッドあり
(スキーマは `codex app-server generate-json-schema` で出力可、experimental)。

---

## 2. 自己調査手順(将来のCLI/モデル変更でズレた時)

上から順に。**推測で書き換えず、必ず実機で確認してから対処すること。**

1. `codex --version` — まずバージョンを見る。0.144.1と違えば以下を再確認
2. `codex exec --help` / `codex --help` — フラグ名・サブコマンドの変化を確認
3. `codex features list` — 機能の追加/削除/デフォルト変化
4. `sh scripts/models.sh list` — 契約上の現行モデルとeffort対応。
   失敗するならapp-serverプロトコル変更 → `codex app-server generate-json-schema` で
   スキーマを取り、`model/list` 相当のメソッド名を探す
5. `codex doctor` — インストール・設定・認証・ランタイムの一括診断
6. 設定キーの疑いがある時 — `-c key=value` で単発上書きして実行ヘッダで効果を確認
   (config.tomlを書き換える前にCLI上書きで検証する)
7. 最後の手段 — バイナリをgrep:
   `grep -aoE "<pattern>" $(find ~/.npm-global -path "*codex-linux*/bin/codex")`
   (client_id・エンドポイント・設定キー名はこの方法で発見済み)
8. 公式ドキュメント: https://developers.openai.com/codex (web_fetchで参照可)

## 3. 認証が切れた時(401)

`ask.sh`/`wait.sh`はcodexの失敗ログ(`could not be refreshed` 等の文字列)を見て
**自動的に手順1へ入る**(`scripts/_lib.sh`の`codex_bridge_report_failure`、
2026-08-27に実機の401で検証済み)。以下は手動で行う場合、またはgrepパターンが
CLI更新でズレた場合の参照用。

優先順:
1. **スマホ完結(PC不要)**: `python3 scripts/login.py gen` で認可URLを生成 →
   ユーザーがブラウザで開きGoogleログイン → `localhost:1455/...` への接続失敗ページの
   **アドレスバーURL全体**をコピーしてチャットに貼ってもらう →
   `python3 scripts/login.py exchange "<URL>"` で auth.json 再生成。
   ※新しいauth.jsonはコンテナ内にしか無いので、ユーザーに知らせて
   skill zipの `auth/auth.json` を更新してもらう(またはClaudeが更新済みzipを再パッケージして渡す)
2. **PCがある場合**: ローカルで `codex login`(Googleでログイン)→
   `~/.codex/auth.json` をskillの `auth/auth.json` に上書き → zip再アップロード

login.py はCLIと同一のOAuthクライアント定数
(client_id `app_EMoamEEZ73f0CkXaXp7hrann`, redirect `http://localhost:1455/auth/callback`,
PKCE S256)を使う。定数がズレたら手順2-7でバイナリから再抽出する。

## 4. 運用上の約束(ユーザー指定)

- モデルは **GPT-5.6 Sol系(またはそれ以降の最新)+ effort xhigh以上のみ**
- APIキー(従量課金)は**いかなる場合も使用しない**
- auth.json・トークン・PATの中身を会話/ログ/成果物に出力しない
- サブスク枠を尊重: 無駄撃ちしない。ただし委譲された大規模作業は遠慮なく実行

---

## 5. セッション機能(検証済み: 2026-07-11)

- 保存先: `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<session_id>.jsonl`
  (1行目 `session_meta` に session_id / timestamp / cwd / cli_version)
- `codex exec` 実行ヘッダの `session id:` 行から現行セッションIDを取得できる。
  `--json` 時はJSONLイベントからも取得可
- 再開:
  - `codex exec resume --last "prompt"` — 直近セッションの継続(同一rolloutに追記される)
  - `codex exec resume <session_id> "prompt"` — 特定セッションの再開(動作確認済み。
    別セッションで調べた内容を正しく想起した)
- **再開時は保存済み履歴をモデルに再投入するため、履歴長に比例してトークンを消費**
  (実測: ごく短い履歴の再開で約7.5k)。長大セッションは要約を新規プロンプトで渡す方が安い
- 会話をまたぐ永続化: サンドボックスリセットで消えるため
  `sessions.sh export`(→ /mnt/user-data/outputs のtar.gzをユーザーが保存)/
  `sessions.sh import <tar.gz>`(次の会話で復元)を使う
- rollout内のユーザーメッセージには `<environment_context>` 等の注入ブロックが混ざる。
  実プロンプト抽出時は先頭が `<` のものをスキップする(sessions.sh list の実装参照)
- 関連サブコマンド: `resume` / `fork`(セッション分岐) / `archive` / `unarchive` / `delete`。
  対話用の `codex resume`(ピッカー)は非対話環境では使わない

## 6. バックグラウンド実行とポーリング(検証済み: 2026-08-27)

### 制約の正体
Claudeのサンドボックスは、Claudeが**ツール呼び出しを終えて発言(ターン)を終了した
瞬間に停止する**。これは実機検証済みで、原因の詳細(VM一時停止か
破棄かプロセスグループごとの回収か)は不明だが、対策は原因によらず同じ:
**ターン内でツール呼び出しを継続し、発言を終わらせない**こと以外に生存手段はない。

### 実装(`ask.sh`のバックグラウンド降格 + `wait.sh`)
- `$CODEX_HOME/bg/<job_id>/` に `pid` / `log`(codexの全stdout+stderr) / `exit_code` /
  `done`(センチネル) / `answer.md`(`-o`の出力先) / `meta`(started_at等)を持つ
- 起動は `setsid sh -c '...' </dev/null >"$JOB_DIR/log" 2>&1 &` で行い、`$!` をPIDとして
  記録。**util-linux 2.39.3のsetsidは対象コマンドをexecで置き換えるだけでforkしない
  (実機確認済み: `$!` が最後まで実プロセスのPIDと一致し、`kill -0 $!` で生死判定できる)**。
  将来util-linuxの挙動が変わり `-f/--fork` 相当がデフォルト化された場合、この前提が
  崩れて「即死んだように見える」誤検知が起きうる → `setsid --version` で確認し、
  ズレていたら `ps -o pid,ppid,pgid,sid,cmd -p $!` で実際にセッションリーダーに
  なっているか確認すること
- 内側の runner スクリプトは **`set -e` を使わない**(`codex exec`が非0で終わった時に
  `ec=$?`以降の後始末行(exit_code書き込み・doneセンチネル)がスキップされるのを防ぐため。
  一度これで「終わらないジョブ」を作って気づいた)
- `wait.sh` は `<job_id> [max_wait_seconds=60] [poll_interval_seconds=5]` を受け取り、
  シェル自身のsleepループで待つ。Claude側の追加トークン消費は「1回の短いツール呼び出し
  + 1行の結果」のみ
- インライン待機の既定値 `CODEX_INLINE_MAX_SECONDS=240`、単発ポーリングの既定
  `max_wait_seconds=60` は**このサンドボックスの1コマンドあたりの実際の上限を計測して
  決めた値ではない**(計測は行っていない・会話を長時間ブロックしないための保守的な
  目安値)。将来、単発の `bash_tool` 呼び出しがこれより早く/遅く打ち切られることが
  観測されたら、この節を更新して値を調整すること

### 認証切れの自動検知(`scripts/_lib.sh`)
`codex_bridge_report_failure()` は失敗したジョブの `log` を
`could not be refreshed|Invalid refresh token|invalid_refresh_token` でgrepし、
一致すれば `login.py gen` をその場で実行する。2026-08-27、実際に期限切れの
auth.jsonに対して実機検証済み(このgrepパターンで捕捉できることを確認)。
CLI更新でエラー文言が変わった場合はこのパターンを更新すること
(`codex exec --help` や実際に401を起こして文言を確認するのが確実)。

### 孤児/クラッシュ検知が`kill -0`単独では機能しない(実機で判明: 2026-08-27)
`ask.sh`がジョブを起動した直後は`setsid`ランナーの親はask.sh自身だが、
**ask.shのプロセスが(インライン待機を終えて)先に終了すると、ランナーは即座に
PID 1へ再親化される**。このサンドボックスのPID 1は子を積極的に回収する
init(tiniやdumb-init相当)ではないらしく、その状態でランナーが死ぬと
**ゾンビ(`<defunct>`, `STAT=Z`)のまま残り続ける**。そして`kill -0`はゾンビPIDに
対しても成功を返す(プロセステーブルにエントリが残っている限り)ため、
「`kill -0`が通れば生きている」という素朴な判定だと**クラッシュしたジョブが
永久に`status=running`と誤判定され続ける**。実際に`kill -9`でランナーを殺して
実機確認して初めて気づいた(`ps -o stat`で`Zs`と表示された)。

対策(`_lib.sh:codex_bridge_job_alive`に実装済み): `kill -0`ではなく
`/proc/<pid>/stat`を直接読み、状態文字(3番目のフィールド。commフィールドは
括弧付きでスペースを含みうるので、最後の`) `より後ろを見て数える)が`Z`なら
「生きていない」扱いにする。PID再利用対策の起動時刻照合も同じ`/proc/<pid>/stat`の
22番目のフィールド(strip後は20番目)`starttime`で行う。この節の一致検証は
`ask.sh`(記録側)と`_lib.sh`(照合側)の両方を変更した時は必ず両方揃えること
(フィールド番号を数え間違えるとstarttimeが常に不一致判定になり、生きている
ジョブまでorphan扱いされる――実装時に一度この番号を数え間違えて気づいた)。

