# codex-bridge

Claude.ai の Skill として動作する、**ChatGPT サブスクリプション枠の Codex CLI を Claude のサンドボックス内から呼び出す**ブリッジです。Claude(Anthropic)と GPT(OpenAI)を 1 つの会話の中で連携させ、知識のセカンドオピニオンや実作業のオフロードを実現します。

> **English summary**: A Claude.ai Skill that runs the official OpenAI Codex CLI inside Claude's code-execution sandbox, authenticated with your own ChatGPT subscription (Codex OAuth, no API key). It lets Claude consult GPT for hard questions or delegate token-heavy work to it — including web search, session resume, and automatic adoption of the newest available model. Verified with codex-cli 0.144.1 (July 2026). Unofficial; use at your own risk.

## 何ができるか

- **知識サポート(相談型)** — Claude が確信を持てない高度・専門的な問題を、web 検索有効の GPT-5.6 Sol(xhigh 以上)に照会し、回答を検証・統合
- **作業オフロード(委譲型)** — 長大な生成・網羅的作業・大量ファイル処理を Codex に委譲。成果物はファイルに書かせ、Claude は検収のみ行うことでトークン消費を抑制
- **セッション機能** — `--continue` / `--resume <id>` で履歴を引き継いだ反復作業。export / import で Claude の会話をまたいだ継続も可能
- **最新モデル自動追随** — app-server の `model/list` API から「xhigh 以上対応の最新モデル」を自動選定。新モデルが契約に追加されれば自動で乗り換え
- **Codex の能力をフル解放** — web 検索 ON、最緩権限、ネストサンドボックス(`codex sandbox`)、ultra effort(サブエージェント自動委譲)に対応
- **スマホ完結の認証** — PC がなくても、Android/iOS の Claude アプリだけで PKCE フローによる auth.json 生成が可能(`scripts/login.py`)

## 仕組み

```
Claude.ai (web / mobile)
 └─ Claude のサンドボックス (code execution)
     ├─ npm install -g @openai/codex        ← setup.sh(セッション内1回)
     ├─ $CODEX_HOME/auth.json               ← skill 同梱の ChatGPT OAuth トークン
     └─ codex exec "プロンプト"              ← ask.sh
         └─ chatgpt.com/backend-api/codex/… ← サブスク枠で GPT-5.6 Sol が応答
```

API キー(従量課金)は一切使いません。スクリプトが `OPENAI_API_KEY` / `CODEX_API_KEY` を強制 unset し、ChatGPT 認証のみで動作します。トークンのリフレッシュは公式 CLI が自動処理します。

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

skill を auth.json なしで一度アップロードし、Claude に「login.py で認証したい」と伝えてください。Claude が認可 URL を生成 → ブラウザでログイン → 失敗ページ(`localhost:1455/...`)の URL を貼り返す、という流れで auth.json を生成できます。詳細は `references/codex-cli-reference.md` の再認証手順を参照。

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
| `scripts/setup.sh` | CLI インストール・auth 配置・設定生成・最新モデル解決(冪等) |
| `scripts/ask.sh` | 実行本体。`"prompt" [xhigh\|max\|ultra] [model]`、`--continue`、`--resume <id>` |
| `scripts/models.sh` | 契約上の利用可能モデル一覧(`list`)と最新モデル自動選定(`resolve`) |
| `scripts/sessions.sh` | セッション一覧・エクスポート・インポート(会話をまたぐ継続用) |
| `scripts/login.py` | PC 不要の PKCE 認証フロー(auth.json の生成・再生成) |

Claude 向けの運用知識(検証済みフラグ・config・app-server RPC・将来の CLI 変更への自己調査手順)は `references/codex-cli-reference.md` に集約しています。

## セキュリティ上の注意

- **`auth/auth.json` はあなたの ChatGPT アカウントのフルアクセストークンです。**
  - このリポジトリには含まれていません(`.gitignore` 済み)。**fork や再配布時も絶対にコミットしないでください**
  - auth.json 入りの skill zip は、自分の Claude.ai アカウント内にのみ留めてください
- Codex の利用はあなたのサブスクリプション枠を消費します(実測目安: 単純応答 約 2.2k / 検索付き 約 6.4k / セッション再開 約 7.5k〜トークン)

## 免責

本ツールは非公式です。OpenAI および Anthropic とは無関係であり、公式に文書化されていない挙動(ChatGPT 認証でのバックエンドルーティング等)に依存しています。各サービスの利用規約はご自身で確認のうえ、自己責任でご利用ください。仕様変更により予告なく動作しなくなる可能性があります(その場合の調査手順は references に記載)。

## 動作確認環境

- codex-cli 0.144.1 / Claude.ai サンドボックス(Ubuntu 24, Node 22, Python 3.12) / 2026-07
- モデル: gpt-5.6-sol(xhigh / max / ultra)で end-to-end 検証済み

## License

[MIT](LICENSE)
