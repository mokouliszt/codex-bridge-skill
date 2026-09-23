---
name: codex-bridge
description: ユーザーのChatGPTサブスクリプション枠(Codex OAuth)で、サンドボックス内から公式Codex CLI(codex exec)を呼び出し、GPTモデルに知識照会や作業委譲を行う。モデルと推論レベルに既定値は無く、ユーザーの明示指定が無ければ実行前にユーザーへ問い直す(選択式UIがあれば優先、モデルは性能上位順に提示)。ユーザーが「Codexに聞いて」「GPTの意見も」「セカンドオピニオン」「Codexにやらせて」等と言及した場合に加え、(a)Claude自身の知識では確信が持てない高度・専門的な問題で照会先が欲しい場合、(b)長大な生成・網羅的作業・大量ファイル処理などClaudeのトークン消費が激しくなる作業をオフロードしたい場合にも、Claudeの判断で必ずこのSkillを使用する。APIキーは使用禁止(サブスク枠のみ)。認証はskill同梱のauth.jsonで完結する(auth/credentials.jsonがあればローテーション後のリフレッシュトークンをS3互換ストレージに永続化)。
---

# codex-bridge — サブスク枠のCodex(GPT)をClaudeの手足・相談役にする

検証済み環境: codex-cli 0.144.1(2026-07-10)/ 0.150.1(2026-08-27, 実行系・認証系のみ再検証)。
いずれもChatGPT認証で `gpt-5.6-sol` end-to-end動作確認済み。
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

## 長時間タスクとサンドボックスの生存(重要・必読)

**codexはClaudeのサンドボックス内のプロセスであり、Claude側のサーバーとは無関係。**
そのため**Claudeがツール呼び出しを終えて発言(ターン)を終了した瞬間、サンドボックス
ごと停止し、実行中のcodexプロセスも道連れで失われる**(実機で確認済みの制約)。
これはバックグラウンド実行にしても回避できない――回避できるのは「ターン内で
ツール呼び出しを継続し、発言を終わらせない」ことだけ。

この制約に対応するため、`ask.sh` は以下のように動く:

1. まず既定240秒(`CODEX_INLINE_MAX_SECONDS`で変更可)だけインラインで待つ。
   この間に終われば**呼び出し方も結果の受け取り方も従来通り**
   (stdout + `/tmp/codex_last.md`)。短い相談(モードA)はほぼ常にここで完結する。
2. 240秒以内に終わらなければ自動的にバックグラウンドジョブへ切り替わり、
   `job_id` を出力して **exit 75** で返る。
   **最初から長時間になるとわかっている場合**(大規模委譲等)は
   `CODEX_ASYNC=1` を付けてインライン待機を省略してよい。

バックグラウンドへ切り替わったら:

```bash
bash ./skills/codex-bridge/scripts/wait.sh <job_id>          # 既定: 最大60秒だけ待って返る
bash ./skills/codex-bridge/scripts/wait.sh <job_id> 90 5      # 待機上限/ポーリング間隔(秒)を指定
```

`wait.sh` は**このシェルコマンド自身がsleepループしながらPIDの生死を見る**ので、
Claude側が別途sleepしたり長考する必要はない。出力は `status=running ...` または
`status=done ...` の1行だけで、育っていくログを毎回貼り直すことはしない――
これがトークン消費を抑える本体(失敗時のみ簡潔なエラー要約を追加で出す)。

**絶対に守ること: `status=running` である間は、相槌や進捗コメントを書いて
ターンを終わらせず、`wait.sh` を間を置かず呼び出し続けること。**
`status=done` になって初めて `/tmp/codex_last.md` を読み、ユーザーへの返答を
まとめてよい。ポーリング自体はローカルでPIDを見るだけでChatGPTサブスク枠は
消費しない(枠を消費するのは実際のcodex exec呼び出しのみ)。

ジョブの状態を見失った場合(会話が長くなって job_id を忘れた等):

```bash
bash ./skills/codex-bridge/scripts/jobs.sh list     # 全ジョブのid/状態/開始時刻/プロンプト冒頭
bash ./skills/codex-bridge/scripts/jobs.sh clean    # 完了済み(24h超)ジョブの後片付け。任意
```

## セッション機能(会話履歴の保持・再開)

Codexは実行ごとにセッション(全履歴)を `$CODEX_HOME/sessions/` にJSONLで永続化する。
これを使うと**過去のやり取りを引き継いだまま**追加指示ができる(実弾検証済み)。

```bash
bash ./skills/codex-bridge/scripts/ask.sh --continue "追加指示" <effort> <model>        # 直近セッションを継続
bash ./skills/codex-bridge/scripts/sessions.sh list                                    # 一覧(id/日時/cwd/冒頭プロンプト)
bash ./skills/codex-bridge/scripts/ask.sh --resume <session_id> "指示" <effort> <model> # 特定セッションを再開
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

## モデル・推論レベルの決定(ユーザー指定・厳守)

**既定のモデル・effortは存在しない。** `ask.sh` はモデルとeffortの両方が渡されない限り
実行を拒否する(exit 2、セットアップや認証フローにも進まない)。呼び出し前に次の順で決める:

1. **ユーザーがこの会話で明示している場合 → それを使う。**
   モデル名・effortの指定のほか、「おまかせ」「任せる」等の一任も明示とみなし、
   一任された項目はClaudeがタスクに応じて選ぶ。片方だけ明示されている場合は、
   もう片方だけを問う。
2. **明示がない場合 → 実行前にユーザーへ問い直す。**
   Claudeの判断でこのSkillを使う場面(descriptionの(a)(b))も例外ではない。
   - まず `sh ./skills/codex-bridge/scripts/models.sh choices` を実行し、現在契約上
     利用可能なモデルを**性能上位順**で取得する(記憶にあるモデル名を使わない)
   - **選択式でユーザーに問えるツール(選択肢ボタン等)が使える環境ではそれを優先する。**
     無い環境では通常のテキストで問う。どちらの場合も黙って任意の値で実行しない
   - モデルの選択肢は `choices` の順(性能の高いモデルから)に並べる。選択肢数に上限が
     ある場合は上位から収め、それ以外のモデルも任意入力で指定できる旨を添える。
     `choices` の並び順は id のtier名(astra > sol > terra > luna)と世代による
     ヒューリスティックなので、未知のtier名が現れた等で明らかにおかしい場合は
     Claudeの判断で妥当な位置へ並べ替えてよい
   - effortの選択肢は、タスクの難度・規模に見合うものをClaudeが適宜選んで提示する
     (全段階を並べる必要はない。推奨を添えるのは可)
   - 可能ならモデルとeffortを1回の問いでまとめて聞く
   - 選ばれた組み合わせがそのモデルで非対応のeffort(`choices` の efforts 列に無い)
     なら、実行せずに問い直す
3. **一度決まった選択は、同じ会話内の後続呼び出し(再試行・`--continue`・`--resume`
   を含む)に引き継ぐ。** ただしタスクの性質が大きく変わった場合や、失敗を受けて
   別モデル・高いeffortへ変えたくなった場合は、Claudeの判断で変更せず改めて問う。

- モデル名は `models.sh` が返したものだけを使い、推測で作らない。
- **最新モデルへの自動追随・自動フォールバックは行わない。** 旧 `model_cache`・既存
  config・再開セッションの設定は参照しない(`ask.sh` が毎回 `-m` とeffortを明示する)。
- **APIキー使用は禁止**。スクリプトが `OPENAI_API_KEY`/`CODEX_API_KEY` を強制unsetし、
  ChatGPT認証(サブスク枠)のみで動作する

## 使い方

### 1. セットアップ(セッション内で一度だけ、冪等)

```bash
bash ./skills/codex-bridge/scripts/setup.sh
```

npm install(20〜40秒)→ auth.json配置 → config.toml生成(web検索ON。モデル・effortは書かない) → ログイン状態表示。
`command -v codex` で導入済み判定可。

**auth.jsonがskill同梱にもCODEX_HOMEにも一切無い場合**(公開リポジトリからの新規
チェックアウト等、認証情報が意図的に外されている状態)、setup.shは警告を出すだけで
終わらず、**その場で`login.py gen`まで自動実行し認可URLを出力する**。表示された
URL・手順をそのままユーザーへ中継し、貼り返された失敗ページのURLで
`python3 scripts/login.py exchange "<URL>"` を実行すればよい。auth.jsonが無いまま
`ask.sh`がcodex execを試みることはない(setup.sh後もauth.jsonが無ければexit 3で
明示的に止まり、生のCLIエラーは出さない)。

### 1b. リフレッシュトークンの永続化(token store、任意)

ChatGPTのリフレッシュトークンは**使い捨て(ローテーション式)**。リフレッシュのたびに新しい
トークンが発行され、古いものは無効になる。skill同梱の `auth/auth.json` は読み取り専用、
サンドボックスの `$CODEX_HOME/auth.json` は会話終了で消えるため、何もしないと
「同梱トークンは最初の1回のリフレッシュで使い切り → 次の会話で再ログイン」になる
(CLIは発行から約8日で自動リフレッシュする)。

`auth/credentials.json`(S3互換ストレージの認証情報)が
同梱されていれば、`scripts/token_store.py` が**S3互換バケットの1オブジェクト**
(既定キー `_codex-bridge/auth.json`)に最新のauth.jsonを保存・取得し、会話をまたいで
トークンを生かし続ける。**無ければ全処理がスキップされ従来どおり動く**(boto3も入れない)。
Claude側で呼び出す必要は基本的に無い:

- `setup.sh` → `token_store.py sync`: 同梱/保存済みのうち新しい方を採用し、古ければ
  (発行7日超 or アクセストークン残り2日未満)その場でリフレッシュして即保存
- `ask.sh` のジョブ終了時(成功・失敗問わず、`done` 書き込み前)→ `push`:
  実行中にCLIがローテーションしていれば保存(変化なしなら無言)
- 認証切れ検知時 → `recover`: 別の会話が既にローテーション済みで保存側が新しければ
  それを採用し `auth_status=recovered` を返す。**この場合は再ログイン不要で、同じ
  `ask.sh` を再実行するだけ**。保存側も無効なら従来どおり `login.py gen` へ
- `login.py exchange` 成功時 → `push`(新規ログインの結果を即保存)

状態確認: `python3 ./skills/codex-bridge/scripts/token_store.py status`(日付のみ出力)。
token storeのエラーは警告を出すだけで、ask.sh等の処理は止めない。
保存オブジェクトが壊れていれば(JSON不正・refresh token無し等)、`sync`/`push` が有効な
ローカルトークンで上書きして自己修復する。

### 2. 実行

```bash
bash ./skills/codex-bridge/scripts/models.sh choices                              # 提示用モデル一覧(性能上位順)
bash ./skills/codex-bridge/scripts/ask.sh "プロンプト" <effort> <model>            # 例: "…" medium gpt-6-astra
CODEX_MODEL=<model> bash ./skills/codex-bridge/scripts/ask.sh "プロンプト" <effort> # env指定も可(第3引数が優先)
bash ./skills/codex-bridge/scripts/ask.sh --continue "追加指示" <effort> <model>    # 直前セッション継続
bash ./skills/codex-bridge/scripts/ask.sh --resume <id> "追加指示" <effort> <model> # 特定セッション再開
```

- 最終回答: stdout + `/tmp/codex_last.md`(長い場合はファイルを読む方が確実)
- `<effort>` / `<model>` はユーザーが明示または選択したもの(「モデル・推論レベルの決定」節)。
  web検索は既定で有効
- env: `CODEX_MODEL=<id>` ユーザーが選んだモデル(第3引数の代わり) / `CODEX_JSON=1` JSONLイベント出力 /
  `CODEX_DRYRUN=1` 実行せずコマンド確認 /
  `CODEX_ASYNC=1` 最初からバックグラウンド実行(長時間タスク前提) /
  `CODEX_INLINE_MAX_SECONDS=<n>` インライン待機の上限(既定240)
- 240秒(既定)を超える、またはCODEX_ASYNC=1の場合は `job_id` を返してバックグラウンドへ
  切り替わる。**この後の運用は必ず「長時間タスクとサンドボックスの生存」節に従うこと**
  (ターンを終わらせずwait.shを呼び続ける)
- 権限は最も緩い構成(`--dangerously-bypass-approvals-and-sandbox` +
  `danger-full-access` + approval never)で固定済み。CLI側で「外部サンドボックス内での
  実行用」と明記されたフラグであり、Claudeサンドボックス内なので適切

### 3. Codexの能力を限界まで使う

- **web検索**: config で既定ON(検証済み)。明示的に検索を促すプロンプトが有効
- **ネストサンドボックス**: Codexはbubblewrap同梱で `codex sandbox <cmd>` により
  自前の隔離環境をさらに展開できる(動作確認済み)。危険な実験をCodex自身に隔離させたい時に
  プロンプトで指示可能
- **ultra effort**: ユーザーが明示または選択した場合のみ利用可(サブエージェント自動委譲。対応モデルは `models.sh choices` の efforts 列で確認)
- **その他stable機能**: browser_use / computer_use / image_generation / hooks / apps 等
  (`codex features list` で確認)。必要に応じてプロンプトから利用を促せる

## クォータ配慮

利用枠はユーザーのChatGPTサブスクを消費する(実測目安: 単純応答 約2.2k / 検索付き応答 約6.4k / セッション再開 約7.5k〜(履歴長に比例))。
- 1タスク=1〜数回のexecに留め、無意味な連続実行をしない
- ただしユーザーが明示的に委譲した大規模作業では遠慮は不要(そのためのSkill)

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| `status=failed auth_status=recovered` | token storeに新しいトークンがあり差し替え済み。同じ `ask.sh` をそのまま再実行する(再ログイン不要) |
| 401 / リフレッシュトークン失効(auth.jsonは存在するが無効) | **自動検出済み**: ask.sh/wait.shがcodexの失敗ログを見て検知し、token storeで回復できなければその場で`login.py gen`を実行してURLを出す。ユーザーへ中継→貼り戻されたURLで`login.py exchange`するだけでよい |
| `[token-store] warning: S3 get/put failed (AccessDenied 等)` / `... is missing: ...` | `auth/credentials.json` のendpoint・bucket・キー・権限(対象バケット/プレフィックスへの読み書き)を確認するようユーザーに伝える。処理自体は継続する |
| auth.jsonが最初から存在しない | setup.shが自動的に同じ`login.py gen`フローへ入る(上と同じ手順)。ask.sh exit 3 |
| `ask.sh` が exit 2「model and effort are required」 | モデルまたはeffortが未指定。「モデル・推論レベルの決定」節に従いユーザーへ問い直してから再実行 |
| `ask.sh` が exit 75 | エラーではない。バックグラウンドへ切り替わっただけ。`job_id`を控えて`wait.sh`を呼び続ける(「長時間タスクとサンドボックスの生存」節) |
| `wait.sh` が `status=unknown` | job_idの誤り/期限切れ、または不正な形式(パス区切りを含む等)。`jobs.sh list` で確認 |
| `wait.sh` が `status=crashed exit_code=125` | ジョブプロセスがdone/exit_code書き込み前に消えた(OOM・SIGKILL・PID再利用の誤検出等)。ログtailを見て原因を確認。ポーリングし続ける必要はない、実質的な失敗として扱う |
| `setup.sh` が exit 4 | auth.jsonがAPIキー形式(`OPENAI_API_KEY`が非null)。ユーザー方針違反として自動的にauth.jsonを削除して停止する。ChatGPT認証で作り直すこと |
| `stream disconnected` / Reconnecting | 一時的なネットワーク断。リトライで解消することが多い |
| モデル名エラー(400) | `scripts/models.sh choices` で契約上の有効モデルを確認し、ユーザーに選び直してもらう |
| フラグ/設定キーが効かない | CLI更新で仕様変更の可能性。references の自己調査手順へ |

## 注意

- auth.json・トークン文字列・`auth/credentials.json` の中身を会話/成果物/ログに**絶対に転記しない**(login.py実行時に
  表示されるPKCE verifier・認可URLも同様に扱う――コンテナ再起動対策として画面には出るが、
  会話の要約やメモリへの保存対象にはしない)
- skill配置ディレクトリは読み取り専用のため、実行時状態はすべて `$CODEX_HOME`
  (`/home/claude/.codex-bridge`)に置かれる。バックグラウンドジョブは
  `$CODEX_HOME/bg/<job_id>/`(pid・log・exit_code・done・answer.md・meta)
- `scripts/_lib.sh` は `ask.sh`/`wait.sh` が読み込む共有関数のみのファイルで、
  直接実行するものではない(shebangなし)
