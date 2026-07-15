#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
韓流ニュース自動投稿bot (Bluesky) — 完全無料・依存パッケージなし(標準ライブラリのみ)

やること:
  1. GoogleニュースRSSから韓流/K-POPニュースを取得
  2. 「見出し + 出典 + 記事リンク(カード)」で投稿
     ※記事本文や画像は転載しない = 著作権対策(見出しとリンクのみ = 通常のニュース共有)
  3. 投稿済みIDをファイルに記録して重複投稿を防止
  4. 熱愛・スキャンダル系の見出しはブロックワードで除外(名誉毀損リスク回避)

GitHub Actions の cron で定期実行する想定(サーバー不要・無料)。

必要な環境変数:
  BLUESKY_HANDLE        例: yourname.bsky.social
  BLUESKY_APP_PASSWORD  Bluesky設定 > App Passwords で発行した英数字(xxxx-xxxx-xxxx-xxxx)
                        ※通常のログインパスワードではなくアプリパスワードを使う
任意:
  NEWS_QUERY            既定 "K-POP OR 韓流"(GoogleニュースのORクエリが使える)
  MAX_POSTS_PER_RUN     既定 "5"
  STATE_FILE            既定 "posted.json"
  BLOCK_WORDS           既定は下記。カンマ区切りで見出しに含まれたら投稿しない
"""

import os
import sys
import json
import time
import html
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

BSKY_SERVICE = "https://bsky.social"
USER_AGENT = "kpop-news-bot/1.0"
POST_TEXT_LIMIT = 300  # Blueskyの本文上限。安全側に運用する

# 噂/スキャンダル系を弾く既定ブロックワード(必要に応じて増減してください)
DEFAULT_BLOCK = "熱愛,破局,不倫,交際,薬物,大麻,暴行,逮捕,訴訟,離婚,炎上,誹謗,不倫,ヌード,流出"


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}Z] {msg}", flush=True)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_post_json(url, payload, extra_headers=None, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---- 投稿済み状態(順序付きリストで保持し、古いものからトリム) ----
def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            ids = json.load(f)
            return ids if isinstance(ids, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_state(path, posted_list, keep=3000):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(posted_list[-keep:], f, ensure_ascii=False, indent=0)


# ---- ニュース取得 ----
def fetch_news(query):
    q = urllib.parse.quote(query)
    # 日本語・日本向けのGoogleニュース検索RSS
    url = f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
    log(f"RSS取得: {url}")
    root = ET.fromstring(http_get(url))
    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()  # ※Googleニュースのリダイレクトリンク
        src_el = item.find("source")
        source = src_el.text.strip() if (src_el is not None and src_el.text) else ""
        # Googleニュースのtitleはたいてい "見出し - 媒体名"
        if not source and " - " in title:
            title, source = title.rsplit(" - ", 1)
        title = html.unescape(title).strip()
        source = html.unescape(source).strip()
        guid = (item.findtext("guid") or link).strip()
        if title and link:
            items.append({"id": guid or link, "title": title, "link": link, "source": source})
    log(f"取得 {len(items)} 件")
    return items


def is_blocked(title, words):
    return any(w and w in title for w in words)


# ---- Bluesky ----
def bsky_login(handle, app_password):
    log(f"Blueskyログイン: {handle}")
    return http_post_json(
        f"{BSKY_SERVICE}/xrpc/com.atproto.server.createSession",
        {"identifier": handle, "password": app_password},
    )


def build_text(title, source):
    text = f"{title}\n（{source}）" if source else title
    if len(text) <= POST_TEXT_LIMIT - 1:
        return text
    # 長すぎる見出しは末尾を省略
    reserve = (len(source) + 4) if source else 0
    keep = max(POST_TEXT_LIMIT - 3 - reserve, 20)
    t = title[:keep].rstrip() + "…"
    return f"{t}\n（{source}）" if source else t


def bsky_post(session, item):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    record = {
        "$type": "app.bsky.feed.post",
        "text": build_text(item["title"], item["source"]),
        "createdAt": now,
        "langs": ["ja"],
        # 外部リンクカード(本文の文字数には含まれない)。画像thumbは付けない=著作権対策
        "embed": {
            "$type": "app.bsky.embed.external",
            "external": {
                "uri": item["link"],
                "title": item["title"][:290],
                "description": (item["source"] or "ニュース")[:290],
            },
        },
    }
    return http_post_json(
        f"{BSKY_SERVICE}/xrpc/com.atproto.repo.createRecord",
        {"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
        extra_headers={"Authorization": f"Bearer {session['accessJwt']}"},
    )


def main():
    handle = os.environ.get("BLUESKY_HANDLE")
    app_password = os.environ.get("BLUESKY_APP_PASSWORD")
    if not handle or not app_password:
        log("ERROR: BLUESKY_HANDLE と BLUESKY_APP_PASSWORD を設定してください")
        sys.exit(1)

    query = os.environ.get("NEWS_QUERY", "K-POP OR 韓流")
    max_posts = int(os.environ.get("MAX_POSTS_PER_RUN", "5"))
    state_file = os.environ.get("STATE_FILE", "posted.json")
    block_words = [w.strip() for w in os.environ.get("BLOCK_WORDS", DEFAULT_BLOCK).split(",") if w.strip()]

    posted_list = load_state(state_file)
    posted_set = set(posted_list)

    try:
        items = fetch_news(query)
    except Exception as e:
        log(f"ERROR: RSS取得失敗: {e}")
        sys.exit(1)

    new_items = [
        it for it in items
        if it["id"] not in posted_set and not is_blocked(it["title"], block_words)
    ]
    log(f"新規 {len(new_items)} 件(今回最大 {max_posts} 件)")
    if not new_items:
        log("投稿対象なし。終了。")
        return

    try:
        session = bsky_login(handle, app_password)
    except Exception as e:
        log(f"ERROR: Blueskyログイン失敗: {e}")
        sys.exit(1)

    count = 0
    # 古い順に投稿すると時系列が自然
    for it in reversed(new_items[:max_posts]):
        try:
            bsky_post(session, it)
            posted_list.append(it["id"])
            posted_set.add(it["id"])
            count += 1
            log(f"投稿OK: {it['title'][:40]}")
            time.sleep(3)  # レート配慮
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            log(f"投稿失敗(HTTP {e.code}): {body[:200]}")
        except Exception as e:
            log(f"投稿失敗: {e}")

    save_state(state_file, posted_list)
    log(f"完了。{count} 件投稿。")


if __name__ == "__main__":
    main()
