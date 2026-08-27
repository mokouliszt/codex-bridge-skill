# codex-bridge

*[English README is here (README.md)](README.md)*

Claude.ai の Skill として動作する、**ChatGPT サブスクリプション枠の Codex CLI を Claude のサンドボックス内から呼び出す**ブリッジです。Claude(Anthropic)と GPT(OpenAI)を 1 つの会話の中で連携させ、知識のセカンドオピニオンや実作業のオフロードを実現します。

> **English summary**: See [README.md](README.md).

## 何ができるか

- **知識サポート(相談型)** — Claude が確信を持てない高度・専門的な問題を、web 検索有効の GPT-5.6 Sol(xhigh 以上)に照会し、回答を検証・統合
- **作業オフロード(委譲型)** — 長大な生成・網羅的作業・大量ファイル処理を Codex に委譲。成果物はファイルに書かせ、Claude は検収のみ行うことでトークン消費を抑制
- **Claudeのターン終了を生き延びる** — Claudeのサンドボックスは、ツール呼び出しを終えて発言を終了した瞬間に停止し、実行中の処理(長時間のCodexジョブ含む)も道連れで失われる。`ask.sh`は実行が長引くと自動的にバックグラウンドジョブへ降格して`job_id`を返し、`wait.sh`でClaudeが安価にポーリング(1回の短いツール呼び出し+1行の結果のみ。育つログを毎回貼り直さない)しながらターンを終わらせずに完了を待てる。詳細は[`SKILL.md`](skills/codex-bridge/SKILL.md)を参照
- **セッション機能** — `--continue` / `--resume <id>` で履歴を引き継いだ反復作業。export / import で Claude の会話をまたいだ継続も可能
- **最新モデル自動追随** — app-server の `model/list` API から「xhigh 以上対応の最新モデル」を自動選定。新モデルが契約に追加されれば自動で乗り換え
- **Codex の能力をフル解放** — web 検索 ON、最緩権限、ネストサンドボックス(`codex sandbox`)、ultra effort(サブエージェント自動委譲)に対応
- **スマホ完結の認証** — PC がなくても、Android/iOS の Claude アプリだけで PKCE フローによる auth.json 生成が可能(`scripts/login.py`)
- **認証切れの自動回復** — auth.jsonが最初から存在しない場合も、リフレッシュトークンが失効した場合も、`setup.sh`/`ask.sh`が検知して同じスマホ完結の再認証フローを自動的に開始する(生のCLIエラーを見せない)

## 仕組み

```
Claude.ai (web / mobile)
 └─ Claude のサンドボックス (code execution)
     ├─ npm install -g @openai/codex        ← setup.sh(セッション内1回)
     ├─ $CODEX_HOME/auth.json               ← skill 同梱の ChatGPT OAuth トークン
     └─ codex exec "プロンプト"              ← ask.sh
         └─ chatgpt.com/backend-api/codex/… ← サブスク枠で GPT-5.6 Sol が応答
```

API キー(従量課金)は一切使いません。スクリプトが `OPENAI_API_KEY` / `CODEX_API_KEY` を強制 unset し、ChatGPT 認証のみで動作します。`setup.sh`は環境変数だけでなくauth.jsonの中身がAPIキー形式でないかも検証し、該当すれば拒否します。トークンのリフレッシュは公式 CLI が自動処理します。

## 必要なもの

- Claude.ai の有料プラン(Skill / コード実行が使えること)
- Codex が利用できる ChatGPT プラン(Plus / Pro 等)
- auth.json 取得用の環境(下記いずれか)
  - Codex CLI を一度でも動かせる PC
  - または スマホのみ(login.py フロー)

## セットアップ

### 1. auth.json を用意する

**PC がある場合(最短)**

```bash
npm install -g @openai/codex
codex login       # ブラウザが開くので ChatGPT アカウントでログイン(Google ログイン可)
```

生成された `~/.codex/auth.json`(Windows: `%USERPROFILE%\.codex\auth.json`)を、この skill の `auth/auth.json` にコピーします。

**スマホしかない場合**

skill を auth.json なしで一度アップロードし、Claude に「login.py で認証したい」と伝えてください。Claude が認可 URL を生成 → ブラウザでログイン → 失敗ページ(`localhost:1455/...`)の URL を貼り返す、という流れで auth.json を生成できます。詳細は `references/codex-cli-reference.md` の再認証手順を参照。この流れは**自動的にも起動します** — auth.json が無い、またはリフレッシュトークンが失効していることに気づいた時点で Claude 自身が開始するので、名指しで頼む必要はありません。

### 2. zip 化して Claude.ai にアップロード

```bash
zip -r codex-bridge.skill.zip codex-bridge/
```

Claude.ai の 設定 → 機能(Capabilities)→ Skill からアップロードします。以後、同一アカウントの web / モバイルアプリの両方で使えます。

### 3. 使う

Claude との会話で自然に依頼するだけです。

- 「**Codex に聞いてみて**: ○○の最新のベストプラクティスは?」
- 「このリファクタリング、**Codex にやらせて**。成果物は out/ に」
- 「さっきの Codex セッションの**続きで**、テストも書かせて」

Claude が skill を認識し、`setup.sh` → `ask.sh` を自律的に実行します。

## スクリプト一覧

| スクリプト | 役割 |
|---|---|
| `scripts/setup.sh` | CLI インストール・auth 配置・設定生成・最新モデル解決(冪等)。auth.json がAPIキー形式でないかの検証と、認証情報が無い場合のスマホ完結再認証の自動起動も行う |
| `scripts/ask.sh` | 実行本体。`"prompt" [xhigh\|max\|ultra] [model]`、`--continue`、`--resume <id>`。既定`CODEX_INLINE_MAX_SECONDS`(240秒)までインライン待機し、超えたら自動でバックグラウンドジョブ+`job_id`に降格(`CODEX_ASYNC=1`で最初から降格も可) |
| `scripts/wait.sh` | バックグラウンドジョブをポーリング(`<job_id> [max_wait_seconds] [poll_interval_seconds]`)。呼び出し自体がsleepループで内部的に待つので、Claude側はdoneになるまで呼び直すだけでよく、1回のツール呼び出しあたりのトークン消費は最小限。プロセスが消えたのに完了印が無い場合(クラッシュ/孤児化)も検知する |
| `scripts/jobs.sh` | バックグラウンドジョブの`list`/`clean [max_age_seconds]`。job_idを見失った時の復旧や、完了済みジョブの後片付けに使う |
| `scripts/_lib.sh` | `ask.sh`/`wait.sh`専用の共有関数(直接実行しない): job_id検証、生存/クラッシュ判定、認証失敗の自動回復トリガー |
| `scripts/models.sh` | 契約上の利用可能モデル一覧(`list`)と最新モデル自動選定(`resolve`) |
| `scripts/sessions.sh` | セッション一覧・エクスポート・インポート(会話をまたぐ継続用) |
| `scripts/login.py` | PC 不要の PKCE 認証フロー(auth.json の生成・再生成) |

Claude 向けの運用知識(検証済みフラグ・config・app-server RPC・バックグラウンドジョブの内部実装・将来の CLI 変更への自己調査手順)は `references/codex-cli-reference.md` に集約しています。

## セキュリティ上の注意

- **`auth/auth.json` はあなたの ChatGPT アカウントのフルアクセストークンです。**
  - このリポジトリには含まれていません(`.gitignore` 済み)。**fork や再配布時も絶対にコミットしないでください**
  - auth.json 入りの skill zip は、自分の Claude.ai アカウント内にのみ留めてください
- Codex の利用はあなたのサブスクリプション枠を消費します(実測目安: 単純応答 約 2.2k / 検索付き 約 6.4k / セッション再開 約 7.5k〜トークン)
- `setup.sh`は`$CODEX_HOME`配下(バックグラウンドジョブのディレクトリ含む)に制限的な`umask`と700/600権限を設定します。ジョブのログやプロンプトは会話の他の部分と同様の内容を含みうるためです

## 免責

本ツールは非公式です。OpenAI および Anthropic とは無関係であり、公式に文書化されていない挙動(ChatGPT 認証でのバックエンドルーティング等)に依存しています。各サービスの利用規約はご自身で確認のうえ、自己責任でご利用ください。仕様変更により予告なく動作しなくなる可能性があります(その場合の調査手順は references に記載)。

## 動作確認環境

- codex-cli 0.144.1(2026-07)/ 0.150.1(2026-08、実行系・認証系を再検証)/ Claude.ai サンドボックス(Ubuntu 24, Node 22, Python 3.12)
- モデル: gpt-5.6-sol(xhigh / max / ultra)で end-to-end 検証済み。実際のトークン失効からの自動回復、714秒かかりバックグラウンド/ポーリング経路を実地で通した委譲タスクでも確認済み

## License

[MIT](LICENSE)
