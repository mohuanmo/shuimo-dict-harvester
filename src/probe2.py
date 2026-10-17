#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性探针 2：评论区接口 + 一批候选 RSS/HTML 源的可达性。用完删除。"""
import hashlib
import json
import time
import urllib.parse
import urllib.request
import urllib.error

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
API = "https://api.bilibili.com"

MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
             33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
             26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
             20, 34, 44, 52]


def get(url, ref="https://www.bilibili.com/", timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": ref, "Accept": "*/*",
        "Accept-Encoding": "identity"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        b = r.read()
        return r.status, r.headers.get("Content-Type", ""), b
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), b""
    except Exception as e:
        return -1, str(e), b""


def mixin_key():
    _, _, b = get(API + "/x/web-interface/nav")
    d = json.loads(b)
    img = d["data"]["wbi_img"]["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub = d["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    raw = img + sub
    return "".join(raw[i] for i in MIXIN_TAB)[:32]


def signed(params, key):
    p = dict(params)
    p["wts"] = int(time.time())
    q = "&".join("%s=%s" % (k, urllib.parse.quote(str(v), safe=""))
                 for k, v in sorted(p.items()))
    return q + "&w_rid=" + hashlib.md5((q + key).encode()).hexdigest()


out = []
st, _, b = get(API + "/x/web-interface/popular?ps=1&pn=1")
v = json.loads(b)["data"]["list"][0]
aid, cid = v["aid"], v["cid"]
out.append("video aid=%s cid=%s" % (aid, cid))

try:
    key = mixin_key()
    out.append("mixin_key ok len=%d" % len(key))
except Exception as e:
    out.append("mixin_key fail %r" % e)
    key = ""

if key:
    for mode in (3, 2):
        q = signed({"oid": aid, "type": 1, "mode": mode,
                    "pagination_str": json.dumps({"offset": ""}),
                    "plat": 1, "web_location": 1315875}, key)
        st, ct, b = get(API + "/x/v2/reply/wbi/main?" + q)
        try:
            d = json.loads(b)
            rep = (d.get("data") or {}).get("replies") or []
            out.append("[wbi/main mode=%d] status=%s code=%s replies=%s first=%r" % (
                mode, st, d.get("code"), len(rep),
                (rep[0]["content"]["message"][:30] if rep else "")))
        except Exception:
            out.append("[wbi/main mode=%d] status=%s raw=%r" % (mode, st, b[:80]))

st, ct, b = get(API + "/x/v2/reply?type=1&oid=%s&pn=1&ps=20&sort=2" % aid)
try:
    d = json.loads(b)
    out.append("[/x/v2/reply] status=%s code=%s n=%s" % (
        st, d.get("code"), len(((d.get("data") or {}).get("replies")) or [])))
except Exception:
    out.append("[/x/v2/reply] status=%s raw=%r" % (st, b[:80]))

st, ct, b = get(API + "/x/v2/dm/web/seg.so?type=1&oid=%s&pid=%s&segment_index=1" % (cid, aid))
out.append("[seg.so] status=%s len=%s head=%r" % (st, len(b), b[:24]))

feeds = [
    "https://sspai.com/feed",
    "https://www.chinanews.com.cn/rss/scroll-news.xml",
    "https://feeds.feedburner.com/ruanyifeng",
    "https://36kr.com/feed",
    "https://www.huxiu.com/rss/0.xml",
    "https://www.ifanr.com/feed",
    "https://www.pingwest.com/feed",
    "https://www.gcores.com/rss",
    "https://jandan.net/feed",
    "https://www.guokr.com/rss/",
    "http://www.people.com.cn/rss/politics.xml",
    "http://www.chinadaily.com.cn/rss/china_rss.xml",
    "https://rss.huanqiu.com/rss.xml",
    "https://www.lifeweek.com.cn/rss",
    "https://feedx.net/rss/thepaper.xml",
    "https://www.ithome.com/rss/",
    "https://www.solidot.org/index.rss",
    "https://www.zhihu.com/rss",
    "https://www.sciencenet.cn/xml/news.aspx?news=0",
]
out.append("")
for u in feeds:
    st, ct, b = get(u, ref="")
    has_item = b"<item" in b or b"<entry" in b
    out.append("%-3s %-24s item=%-5s %s | %r" % (st, (ct or "")[:24], has_item, u, b[:60]))

htmls = ["http://www.people.com.cn/", "https://www.thepaper.cn/",
         "https://www.gushiwen.cn/", "https://www.ithome.com/"]
out.append("")
for u in htmls:
    st, ct, b = get(u, ref="")
    out.append("HTML %-3s %s %s bytes" % (st, u, len(b)))

open("probe.txt", "w", encoding="utf-8").write("\n".join(out) + "\n")
print("\n".join(out))
