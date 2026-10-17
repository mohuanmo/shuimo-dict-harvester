#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性探针：看看几种弹幕接口在 CI 里到底返回什么。用完可删。"""
import json, sys, urllib.request, urllib.error

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def get(url, ref="https://www.bilibili.com/"):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": ref, "Accept": "*/*",
        "Accept-Encoding": "identity"})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        b = r.read()
        return r.status, len(b), b
    except urllib.error.HTTPError as e:
        return e.code, 0, b""
    except Exception as e:
        return -1, 0, str(e).encode()


out = []
# 先拿一个热门视频
try:
    d = json.loads(get("https://api.bilibili.com/x/web-interface/popular?ps=1&pn=1")[2])
    v = d["data"]["list"][0]
    aid, cid = v["aid"], v["cid"]
    out.append("video aid=%s cid=%s title=%s" % (aid, cid, v["title"][:30]))
except Exception as e:
    out.append("拿热门失败：%r" % e)
    aid, cid = 0, 0

tests = [
    ("comment.xml", "https://comment.bilibili.com/%s.xml" % cid),
    ("dm.list.so", "https://api.bilibili.com/x/v1/dm/list.so?oid=%s" % cid),
    ("seg.so", "https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid=%s&pid=%s&segment_index=1" % (cid, aid)),
    ("dm.web", "https://api.bilibili.com/x/v2/dm/web/dmseg?type=1&oid=%s&pid=%s" % (cid, aid)),
]
for name, url in tests:
    st, n, b = get(url)
    head = b[:120]
    out.append("[%s] status=%s len=%s head=%r" % (name, st, n, head))

# 试一下 view 接口能不能拿到 cid（作为备用路径）
st, n, b = get("https://api.bilibili.com/x/web-interface/view?aid=%s" % aid)
out.append("[view] status=%s len=%s" % (st, n))
try:
    d = json.loads(b)
    out.append("view code=%s cid=%s desc_len=%s" % (d.get("code"), d.get("data", {}).get("cid"),
                                                    len(d.get("data", {}).get("desc") or "")))
except Exception as e:
    out.append("view parse fail %r" % e)

open("probe.txt", "w", encoding="utf-8").write("\n".join(out) + "\n")
print("\n".join(out))
