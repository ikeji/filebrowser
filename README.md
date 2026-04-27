# fb — mobile file browser

カレントディレクトリをスマホのブラウザから覗くための 1 ファイル Python サーバー。
Python 3.7+ と stdlib のみで動く。外部依存なし（シンタックスハイライトは hljs CDN）。

## 使い方

見たいディレクトリで実行する:

```sh
./server.py
# あるいは PATH に置いて
fb
```

起動するとトークン付き URL がインターフェースごとに出る:

```
[fb] serving: /home/you/work/foo
[fb] eth0    http://192.168.1.23:8472/?t=PLlP_XqNjWDxAYODb1_PHbZx
[fb] tail    http://100.78.24.92:8472/?t=PLlP_XqNjWDxAYODb1_PHbZx
[fb] local   http://127.0.0.1:8472/?t=PLlP_XqNjWDxAYODb1_PHbZx
```

スマホで開く（同じ wifi か tailnet）。最初の URL でトークンを送ると Cookie に入るので、以降はトークンなしで OK。

### オプション

```
--host HOST    bind address (default: 0.0.0.0)
--port PORT    default: 8000-8999 からランダムに空きを探す
--token TOKEN  認証トークン (default: $FB_TOKEN、無ければランダム生成)
```

トークンを固定したい場合（再起動しても同じ URL を使い続けたい等）は環境変数で:

```sh
FB_TOKEN=mysecret fb
```

## 機能

- ファイルツリー（lazy 展開、`.git` `node_modules` などは除外）
- ビューア: シンタックスハイライト、行番号、画面幅で折り返し
- 画像プレビュー (png/jpg/gif/webp/svg/bmp/ico/avif、タップで原寸トグル)
- ピンチで文字サイズ変更
- ブラウザの戻るでファイル一覧へ
- ファイル mtime を long-poll で監視、変更があれば自動リロード
- 横断 grep
- 現在のファイルパスをコピー
- 行番号を長押し → コメント入力 → 次の行に追記（自動リロードで反映）
- リロード後もハッシュで開いていたファイルに復帰

## セキュリティ

- トークン: 起動ごとに `secrets.token_urlsafe(18)`、`secrets.compare_digest` で比較
- パスは `Path.resolve()` でルート脱出を防ぐ
- 認証は token + Cookie のみ。**信頼できないネットワークには出さない**こと（HTTP 平文）。tailnet か LAN 限定で使う前提。
- POST `/api/insert` で書き込みできる。LAN/tailnet 内に許可していない人がいるなら使わない。

## 制限

- 表示するファイルは 2 MiB まで（画像は 20 MiB まで）
- grep 対象は 1 ファイル 4 MiB まで、合計マッチ 500 件で打ち切り

## リリース・配布

**par / pex / zipapp は不要**。

理由:

- もともと 1 ファイル
- stdlib しか使っていない（CDN の hljs は別途ネット接続が要るが、それは bundle してもどうしようもない）
- 配るなら `server.py` を `chmod +x` して、PATH 上に `fb` という名前でシンボリックリンクすれば十分

```sh
chmod +x server.py
ln -s "$PWD/server.py" ~/bin/fb
```

zipapp 化は依存をまとめるための仕組みなので、依存ゼロのこのスクリプトでは利点がなく、むしろシバンの取り回しや mtime 監視（`__file__` がアーカイブ内になる）でハマる可能性がある。
