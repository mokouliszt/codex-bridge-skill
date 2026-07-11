---
name: codex-bridge
description: ユーザーのChatGPTサブスクリプション枠(Codex OAuth)で、サンドボックス内から公式Codex CLI(codex exec)を呼び出し、GPT-5.6 Sol(xhigh以上)に知識照会や作業委譲を行う。ユーザーが「Codexに聞いて」「GPTの意見も」「セカンドオピニオン」「Codexにやらせて」等と言及した場合に加え、(a)Claude自身の知識では確信が持てない高度・専門的な問題で照会先が欲しい場合、(b)長大な生成・網羅的作業・大量ファイル処理などClaudeのトークン消費が激しくなる作業をオフロードしたい場合にも、Claudeの判断で必ずこのSkillを使用する。APIキーは使用禁止(サブスク枠のみ)。認証はskill同梱のauth.jsonで完結する。
---

# codex-bridge — サブスク枠のCodex(GPT-5.6 Sol)をClaudeの手足・相談役にする

検証済み環境: codex-cli 0.144.1 / 2026-07-10 / ChatGPT認証で `gpt-5.6-sol` end-to-end動作確認済み。
将来のCLI/モデル変更で挙動がズレたら `references/codex-cli-reference.md` の自己調査手順に従うこと。

## このSkillの2つの運用モード

### モードA: 知識サポート(相談型)
Claude(特に軽量モデル)が高度な知識・難問の照会先としてCodexを使う。
- 質問+必要なコンテキストをプロンプトで渡し、回答を得る
- web検索は既定で有効なので「必要なら最新情報をwebで確認して」と促すと精度が上がる
- 回答は `/tmp/codex_last.md` から取得し、Claudeが検証・統合して人間に返す

### モードB: 作業オフロード(委譲型)
Claude(特に高知識・高単価モデル)がトークン消費の激しい実作業をCodexに委譲する。
- 作業ディレクトリに `cd` してから呼ぶ(Codexはそのディレクトリを読み書きできる)
- **成果物は必ずファイルに書かせる**(「結果は ./out/ 配下に保存して」等)
- Claudeは成果物ファイルを検収し、必要箇所だけ読む。**Codexの長い出力を会話に丸写ししない**
  (これがトークン節約の本体。ユーザーへはファイルをそのまま提示すればよい)
- 反復修正はセッション機能で履歴を引き継ぐ(次節)

## セッション機能(会話履歴の保持・再開)

Codexは実行ごとにセッション(全履歴)を `$CODEX_HOME/sessions/` にJSONLで永続化する。
これを使うと**過去のやり取りを引き継いだまま**追加指示ができる(実弾検証済み)。

```bash
bash ./skills/codex-bridge/scripts/ask.sh --continue "追加指示"        # 直近セッションを継続
bash ./skills/codex-bridge/scripts/sessions.sh list                    # 一覧(id/日時/cwd/冒頭プロンプト)
bash ./skills/codex-bridge/scripts/ask.sh --resume <session_id> "指示" # 特定セッションを再開
```

使い分け:
- **単線の反復修正** → `--continue`(直近1本を追いかける)
- **複数作業の並行管理** → 各作業の開始時に実行ヘッダの `session id:` を控える
  (または `sessions.sh list` で確認)→ `--resume <id>` で対象を選んで継続
- 新規タスクは新規セッションで始める(無関係な履歴の引き継ぎはトークンの無駄)

コストと寿命の注意:
- **再開時は履歴の再投入分トークンを消費する**(実測: 短い履歴の再開で約7.5k)。
  長大セッションの安易な再開はクォータを食うので、要点を新規プロンプトに要約して
  渡す方が安い場面も多い
- セッションはClaudeの**会話が変わると消える**(サンドボックスリセット)。会話をまたいで
  継続したい場合は `sessions.sh export` でtar.gzを出力しユーザーに保存してもらい、
  次の会話で `sessions.sh import <アップロードされたtar.gz>` で復元 → `--resume <id>`
- `codex exec resume --last` / `resume <id>` の他、`fork`(枝分かれ)・`archive`・`delete`
  等のセッション管理サブコマンドもある(references参照)

## モデルポリシー(ユーザー指定・厳守)

- **既定: `gpt-5.6-sol` + `xhigh`**。effortは xhigh / max / ultra のみ許可(xhigh未満は使わない)
  - `max`: 最難問向け
  - `ultra`: 最大reasoning+自動タスク委譲(Codexが内部でサブエージェント展開)。大規模作業のオフロード時に有効
- **新モデル追随**: セットアップ時に `scripts/models.sh resolve` が model/list APIから
  「xhigh以上対応の最新版(例: gpt-5.7系が出たらそれ)」を自動選定してキャッシュする。
  手動確認は `scripts/models.sh list`
- **APIキー使用は禁止**。スクリプトが `OPENAI_API_KEY`/`CODEX_API_KEY` を強制unsetし、
  ChatGPT認証(サブスク枠)のみで動作する

## 使い方

### 1. セットアップ(セッション内で一度だけ、冪等)

```bash
bash ./skills/codex-bridge/scripts/setup.sh
```

npm install(20〜40秒)→ auth.json配置 → config.toml生成 → 最新モデル解決 → ログイン状態表示。
`command -v codex` で導入済み判定可。

### 2. 実行

```bash
bash ./skills/codex-bridge/scripts/ask.sh "プロンプト"                # sol + xhigh + web検索
bash ./skills/codex-bridge/scripts/ask.sh "プロンプト" max            # effort指定
bash ./skills/codex-bridge/scripts/ask.sh "プロンプト" ultra          # 自動タスク委譲
bash ./skills/codex-bridge/scripts/ask.sh --continue "追加指示"       # 直前セッション継続
bash ./skills/codex-bridge/scripts/ask.sh --resume <id> "追加指示"    # 特定セッション再開
```

- 最終回答: stdout + `/tmp/codex_last.md`(長い場合はファイルを読む方が確実)
- env: `CODEX_MODEL=<id>` モデル上書き / `CODEX_JSON=1` JSONLイベント出力 /
  `CODEX_DRYRUN=1` 実行せずコマンド確認
- 権限は最も緩い構成(`--dangerously-bypass-approvals-and-sandbox` +
  `danger-full-access` + approval never)で固定済み。CLI側で「外部サンドボックス内での
  実行用」と明記されたフラグであり、Claudeサンドボックス内なので適切

### 3. Codexの能力を限界まで使う

- **web検索**: config で既定ON(検証済み)。明示的に検索を促すプロンプトが有効
- **ネストサンドボックス**: Codexはbubblewrap同梱で `codex sandbox <cmd>` により
  自前の隔離環境をさらに展開できる(動作確認済み)。危険な実験をCodex自身に隔離させたい時に
  プロンプトで指示可能
- **ultra effort**: Codex内部のサブエージェント自動委譲。並列調査・大規模リファクタ向き
- **その他stable機能**: browser_use / computer_use / image_generation / hooks / apps 等
  (`codex features list` で確認)。必要に応じてプロンプトから利用を促せる

## クォータ配慮

利用枠はユーザーのChatGPTサブスクを消費する(実測目安: 単純応答 約2.2k / 検索付き応答 約6.4k / セッション再開 約7.5k〜(履歴長に比例))。
- 1タスク=1〜数回のexecに留め、無意味な連続実行をしない
- ただしユーザーが明示的に委譲した大規模作業では遠慮は不要(そのためのSkill)

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| 401 Unauthorized | auth.json失効。`scripts/login.py` でスマホ完結の再認証(references参照)、またはPCで `codex login` → auth.json差し替え |
| `stream disconnected` / Reconnecting | 一時的なネットワーク断。リトライで解消することが多い |
| モデル名エラー(400) | `scripts/models.sh list` で契約上の有効モデルを確認 |
| フラグ/設定キーが効かない | CLI更新で仕様変更の可能性。references の自己調査手順へ |

## 注意

- auth.json・トークン文字列を会話/成果物/ログに**絶対に転記しない**
- skill配置ディレクトリは読み取り専用のため、実行時状態はすべて `$CODEX_HOME`
  (`/home/claude/.codex-bridge`)に置かれる
