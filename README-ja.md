# indoor-generator

[English](README.md) | 日本語

> 個人の独立したプロジェクトです。Mapbox の公式製品ではなく、Mapbox によるサポート・承認を受けたものではありません。Mapbox は Mapbox, Inc. の商標です。

実在する建物の屋内マップ（[Mapbox indoor データモデル](https://docs.mapbox.com/help/dive-deeper/indoor-mapping/) の GeoJSON:
`facility` > `structure` > `floor` > `room`）を、自然言語のリクエストから生成し、会話で改善していくためのツールです。

```
「丸ビルの屋内マップを作って」
  → OSM の footprint + Web 調査（公式フロアガイド等）
  → configs/<slug>.json
  → Python パイプライン
  → output/<slug>/indoor.geojson + ライブプレビュー
  →「3階の東側をカフェに」などの指示で改善
```

調査・config の記述・対話は Claude Code（`.claude/skills/` のスキル）が担当し、config からの生成は決定的な Python パイプラインが担当します。
設計の詳細は [docs/architecture-ja.md](docs/architecture-ja.md)（日本語）を参照してください。

## クイックスタート

このツールは、[Claude Code](https://code.claude.com/docs/ja/overview) に話しかけて使います。**このリポジトリを clone したフォルダの中で** Claude Code を起動することが前提です。
建物を調べてパイプラインを動かす方法を知っているスキル（`.claude/skills/` と `CLAUDE.md`）はこのリポジトリの中にあり、Claude Code は起動したフォルダの分だけを読み込みます。
別のフォルダで起動すると、このツールのことは何も知りません。

1. Claude Code をインストールしてサインインします（ターミナルの CLI、デスクトップアプリ、VS Code 拡張のどれでも使えます。[ドキュメント](https://code.claude.com/docs/ja/overview)参照）。Python 3.10 以上（開発に使った版。古い版は未確認）と git も必要です。以下のコマンドは macOS / Linux を想定しています（Windows では `.venv\Scripts\...`）。
2. このリポジトリを clone してセットアップします（詳細は[セットアップ](#セットアップ)）。

   ```sh
   git clone https://github.com/akiramur/indoor-generator.git
   cd indoor-generator
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp .env.example .env   # MAPBOX_ACCESS_TOKEN に Mapbox の public トークン（pk.*）を入れる
   ```

3. **そのフォルダの中で** Claude Code を起動します（デスクトップアプリや VS Code では、clone したフォルダをプロジェクトとして開きます）。

   ```sh
   claude
   ```

4. Claude Code のプロンプト（シェルではなくチャット）に依頼を入力します。例:

   ```
   丸ビルの屋内マップを作って
   ```

   Claude Code が建物を調べ、`configs/<slug>.json` を書き、`output/<slug>/indoor.geojson` をビルドして、ライブプレビュー（<http://127.0.0.1:8765>）を起動します。
   修正も同じセッションで頼みます（「2階の北側を会議室に」など）。
   コマンドの実行や Web ページの取得の前に許可を求められることがあります。内容を読んで、よいものを承認してください。

Claude Code の利用には Claude のサブスクリプションまたは API アカウントが必要で、利用枠を消費します。1 棟の調査とビルドには、数分間の Web 検索・取得が伴います（`main.py cost` が API 定価での推定額を出します）。

Claude Code なしでパイプラインだけ試すには、[サンプルを動かす](#サンプルを動かす)を見てください。

## セットアップ

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # トークンを記入
```

| 変数 | 用途 |
|---|---|
| `MAPBOX_ACCESS_TOKEN` | 公開用 `pk.*` トークン。プレビューの `index.html` に埋め込まれる（未設定ならページで入力） |
| `MAPBOX_UPLOAD_TOKEN` | **secret** `sk.*`（tilesets:read / write / list）。`publish --upload` だけで使い、どこにも埋め込まない |

`.env` は git 管理外です。シェルの環境変数が優先されます。`pk.*` トークンは `output/<slug>/index.html` に書き込まれます。このファイルを人に渡すときは、Mapbox のコンソールでトークンに URL 制限を付けてください。

### Mapbox アカウントとトークン

1. [mapbox.com](https://account.mapbox.com/auth/signup/) で無料登録してサインインします。登録時に決めたユーザー名が Mapbox ユーザー名です。
2. [Access tokens](https://console.mapbox.com/account/access-tokens/) を開きます。プレビューには **Default public token**（`pk.*`）で足りるので、`MAPBOX_ACCESS_TOKEN` に設定します。
3. タイルセットをアップロードする場合だけ、**Create a token** で `tilesets:read` / `tilesets:write` / `tilesets:list` にチェックを入れて作成し、**secret** トークン（`sk.*`。表示は一度だけ）を `MAPBOX_UPLOAD_TOKEN` に設定します。コミットしたり Web ページに埋め込んだりしないでください。

### `publish.json`（`publish` / `viewer` を使うときだけ）

`main.py publish` は、ビルド済みの全建物を1つのタイルセットとしてアップロードします。そのタイルセット ID は `<Mapbox ユーザー名>.<タイルセット名>` です。
ユーザー名は秘密ではありませんが人ごとに違うため、リポジトリには固定せず、サンプルをコピーして編集します（git 管理外）。
このファイルが無いと `publish` はユーザー名を持てません（プレビューと `build` には不要です）。

```sh
cp publish.example.json publish.json
```

```json
{
  "username": "your-mapbox-username",
  "default_target": "dev",
  "targets": { "dev": "indoor-dev", "prod": "indoor" },
  "minzoom": 14,
  "maxzoom": 16
}
```

| キー | 意味 |
|---|---|
| `username` | Mapbox アカウントのユーザー名（タイルセットの所有者） |
| `targets` | ターゲット名 → タイルセット名。`dev` は `<username>.indoor-dev`、`prod` は `<username>.indoor` として公開されます |
| `default_target` | `--target` を省略したときの宛先。本番を誤って更新しないよう `dev` のままにし、本番は `--target prod` と明示したときだけ |
| `minzoom` / `maxzoom` | 生成するタイルのズーム範囲 |

`--upload` を付けない限り `publish` はドライランです。

## 使い方

### Claude Code から（通常の使い方）

このリポジトリの中で起動した Claude Code のセッション（[クイックスタート](#クイックスタート)）で、建物名を伝えます。`indoor-map` スキルが調査から config 作成・ビルド・プレビュー起動までを行います。

- 作成: 「丸ビルの屋内マップを作って」
- 編集: 「2階の北側を会議室に」、プレビューで部屋をクリックして「この部屋を広く」、「元に戻して」
- フロアマップ画像: 「この画像を3階に重ねて」（`indoor-floor-image`）
- 出典の確認: 「出典の弱いところを調べて」「いくらかかった？」（`indoor-quality`）
- 公開: 「アップロードして」（`indoor-publish`。依頼したときだけ実行される）

### サンプルを動かす

```sh
.venv/bin/python main.py build samples/building_config.json
.venv/bin/python main.py serve marubiru-sample   # http://127.0.0.1:8765
```

プレビューは Mapbox の `IndoorControl`（右下、ズーム 16 以上）で階を切り替えます。
プレビューは日本語・英語に対応しています。初期表示は英語で、右上のボタンで日本語に切り替えます（選択は保存され、URL の `?lang=en` / `?lang=ja` で固定もできます）。部屋名などの建物のデータはそのままです。

### 主なコマンド

すべて結果を JSON で標準出力に返します（`report` だけは既定でテキスト表。`--json` で JSON）。

| コマンド | 内容 |
|---|---|
| `locate` / `parts` | OSM の建物候補、`building:part` の取得 |
| `validate` / `build` / `serve` / `undo` | config の検証、生成、ライブプレビュー、1 版戻す |
| `import-imagemap` | HTML イメージマップ形式のフロアガイドから実際のテナント形状を取り込む（サイト専用の importer は `indoor_gen/importers/*_cli.py` として追加できます。`indoor-importer` スキル参照） |
| `basemap` / `landmarks` / `align` / `trace` | フロアマップ画像の配置・位置合わせ・トレース |
| `edit-rooms` | プレビューでの部屋の形の手編集（頂点の移動・追加・削除、部屋の削除・追加、複数の部屋の結合。離れた部屋は 5 m までのすき間を埋めて結合）を反映して build。「真上から」ボタンは、表示中のフロアマップの上が画面の上になる向きに回す |
| `nudge-map` | 建物の全 plan とフロアマップを、回転（反時計回り）・拡大縮小・東西南北の移動で少しだけ動かす（プレビューのスライダーからも同じことができる） |
| `mapbox-footprint` | OSM 外枠の代わりに Mapbox の Building（Mapbox Streets の `building` レイヤー）の形を外枠にする。`--realign` で plan とフロアマップも合わせて動かす（相似変換 1 つ）、`--dry-run` は報告のみ |
| `report` / `compare` / `cost` | 出典の確度、2 ビルドの比較、Claude 利用コストの推定 |
| `publish` / `viewer` | 全建物を 1 つのタイルセットへ公開（既定はドライラン・dev）、公開結果の確認 |

`main.py <command> --help` で各オプションを確認できます。

## ディレクトリ

| パス | 内容 |
|---|---|
| `configs/<slug>.json` | 建物 config（source of truth。`output/` は直接編集しない。公開リポジトリには含まれない。`indoor-map` スキルで作るか `samples/` から始める） |
| `configs/plans/<slug>/` | フロアごとの plan GeoJSON（実形状の部屋） |
| `publish.json` | publish のローカル設定（git 管理外。`publish.example.json` をコピー） |
| `output/<slug>/` | 生成物（git 管理外） |
| `indoor_gen/` | パイプライン本体 |
| `tools/` | フロアマップ画像を読むための補助スクリプト（ピクトグラム候補の検出、トレースの重ね合わせ、格子付き切り出し、プレビューのスクリーンショット） |
| `.claude/skills/` | Claude Code 用スキル（`indoor-*` と、公式からコピーした `mapbox-*`） |
| `samples/` | サンプル config |
| `tests/` | pytest |
| `docs/` | アーキテクチャ文書など |

## インポーターと第三者データ

`import-imagemap`（とサイト専用の importer）は商業施設サイトが公開しているフロアガイドのページを読みます。使う前に各サイトの利用規約と
robots.txt を確認してください。取得は素の User-Agent で行い、ダウンロードはすべて `output/` にキャッシュします（たまに個人で使う想定です）。
第三者の画像や SVG はコミットしないでください（`configs/plans/*/maps/` は git 管理外。config には URL だけを残します）。
`mapbox-footprint` は自分のトークンで Mapbox Streets のデータを使います（Mapbox の利用規約の範囲で）。

第三者のフロアガイドやフロアマップ画像から作ったマップ（取り込んだ形状、トレース結果、公開したタイルセット）は二次的著作物になりえます。共有・公開する前に、権利者の許諾を得てください。

## テスト

```sh
.venv/bin/python -m pytest -q
```

## 注意

- **精度。** マップは Web 調査と推定から作られた近似です（`estimated` / `listed` の部屋は形に出典がありません）。ナビゲーション、避難など、安全に関わる用途には使わないでください。現状のまま提供し、保証はありません（`LICENSE` 参照）。
- 公開（`publish --upload`）は全建物で 1 つのタイルセットを置き換えます。ローカルの `output/` が不完全だと建物が消えるので、先にドライランで一覧を確認してください。本番へは `--target prod` が必要です。
- 公式サイトのフロアマップ画像など第三者の素材はリポジトリにコミットしません（config は URL を保持し、画像は `output/` にキャッシュされます）。
- 地図データ: footprint や座標は © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors（ODbL）と、`mapbox-footprint` 使用時は Mapbox Streets（Mapbox の利用規約）に由来します。このツールで作った地図を公開するときは帰属表示を残してください。
- プレビューサーバーは手元の config や plan を書き換えるため、localhost 宛のリクエストだけを受け付け、他サイトからの POST は拒否します。`serve --host 0.0.0.0` はネットワークに公開します（認証なし）。信頼できるネットワークでだけ使ってください。
- OSM の API は Nominatim の 1 req/s 制限を守って利用します。footprint は `output/<slug>/footprint_cache.geojson` にキャッシュされます。
