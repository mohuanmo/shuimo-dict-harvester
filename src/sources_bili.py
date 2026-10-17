#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B 站文本源（不带登录态）
------------------------
    bilibili_popular   热门列表：标题/简介/分区/UP主
    bilibili_ranking   排行榜
    bilibili_comments  评论区：wbi 签名的 /x/v2/reply/wbi/main（口语金矿）
    bilibili_danmaku   弹幕：/x/v2/dm/web/seg.so（protobuf）

所有请求都走 fetchx：状态码、content-type、错误页、空 data 都会被拦下来，
抓不到就抛 SourceRejected，绝不让半截数据进词库。
"""

import hashlib
import json
import re
import time
import urllib.parse

import fetchx

API = "https://api.bilibili.com"

# 评论/弹幕里的 [笑哭] [吃瓜] 这类表情占位符、零宽字符、多余空白
_EMOJI = re.compile(r"\[[^\]]{1,8}\]")
_ZW = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff]")


def clean_text(s):
    s = _EMOJI.sub("", s or "")
    s = _ZW.sub("", s)
    return re.sub(r"\s+", " ", s).strip()

# wbi 混排表（B 站前端固定常量）
MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
             33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
             26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
             20, 34, 44, 52]

_nav = {"key": None, "ts": 0}


def mixin_key(force=False):
    if not force and _nav["key"] and time.time() - _nav["ts"] < 3600:
        return _nav["key"]
    d = fetchx.get_json(API + "/x/web-interface/nav", allow_empty=True, allow_code=True)
    img = d["data"]["wbi_img"]["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub = d["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    raw = img + sub
    if len(raw) < 64:
        raise fetchx.SourceRejected("wbi_key 拿不到")
    _nav.update(key="".join(raw[i] for i in MIXIN_TAB)[:32], ts=time.time())
    return _nav["key"]


def sign(params, key=None):
    key = key or mixin_key()
    p = dict(params)
    p["wts"] = int(time.time())
    q = "&".join("%s=%s" % (k, urllib.parse.quote(str(v), safe=""))
                 for k, v in sorted(p.items()))
    return q + "&w_rid=" + hashlib.md5((q + key).encode()).hexdigest()


# ---------------------------------------------------------------- 列表类

def _lines_of_video(v):
    out = []
    for k in ("title", "desc", "dynamic", "tname"):
        s = v.get(k)
        if isinstance(s, str) and s.strip():
            out.append(s.strip())
    owner = v.get("owner") or {}
    if owner.get("name"):
        out.append(str(owner["name"]))
    for t in (v.get("tags") or []):
        if isinstance(t, dict) and t.get("tag_name"):
            out.append(t["tag_name"])
    return out


def popular(pages=2, ps=50):
    lines, vids = [], []
    for pn in range(1, pages + 1):
        d = fetchx.get_json(API + "/x/web-interface/popular?ps=%d&pn=%d" % (ps, pn))
        lst = (d.get("data") or {}).get("list") or []
        if not lst:
            raise fetchx.SourceRejected("热门列表为空")
        for v in lst:
            lines += _lines_of_video(v)
            if v.get("cid") and v.get("aid"):
                vids.append((int(v["aid"]), int(v["cid"])))
    return lines, vids


def ranking(rid=0):
    d = fetchx.get_json(API + "/x/web-interface/ranking/v2?rid=%d&type=all" % rid)
    lst = (d.get("data") or {}).get("list") or []
    if not lst:
        raise fetchx.SourceRejected("排行榜为空")
    lines, vids = [], []
    for v in lst:
        lines += _lines_of_video(v)
        if v.get("cid") and v.get("aid"):
            vids.append((int(v["aid"]), int(v["cid"])))
    return lines, vids


# ---------------------------------------------------------------- 评论区

def comments(vids, pages=2, delay=0.5):
    """热门评论（mode=3）+ 最新评论（mode=2），翻页用 cursor"""
    key = mixin_key()
    lines = []
    for aid, _cid in vids:
        for mode in (3, 2):
            offset = ""
            for _ in range(pages):
                q = sign({"oid": aid, "type": 1, "mode": mode,
                          "pagination_str": json.dumps({"offset": offset}),
                          "plat": 1, "web_location": 1315875}, key)
                try:
                    d = fetchx.get_json(API + "/x/v2/reply/wbi/main?" + q)
                except fetchx.SourceRejected as e:
                    if "wbi" in str(e) or "-403" in str(e):
                        raise
                    break
                data = d.get("data") or {}
                for r in (data.get("top_replies") or []) + (data.get("replies") or []):
                    msg = clean_text((r.get("content") or {}).get("message"))
                    if msg:
                        lines.append(msg)
                        # 楼中楼往往更口语
                        for sub in ((r.get("replies") or [])[:3]):
                            m2 = clean_text((sub.get("content") or {}).get("message"))
                            if m2:
                                lines.append(m2)
                cur = data.get("cursor") or {}
                if cur.get("is_end"):
                    break
                offset = ((cur.get("pagination_reply") or {}).get("next_offset") or "")
                if not offset:
                    break
                time.sleep(delay)
        time.sleep(delay)
    # 置顶评论会和普通评论重复，去个重
    seen, uniq = set(), []
    for s in lines:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    if not uniq:
        raise fetchx.SourceRejected("评论区一条都没拿到")
    return uniq


# ---------------------------------------------------------------- 弹幕（protobuf）

def _varint(buf, i):
    shift = 0
    val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _msg_string(buf, want_field):
    """从一条 protobuf 消息里取指定字段的字符串值"""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        f, wt = key >> 3, key & 7
        if wt == 2:
            ln, i = _varint(buf, i)
            val = buf[i:i + ln]
            i += ln
            if f == want_field:
                return val.decode("utf-8", "ignore")
        elif wt == 0:
            _, i = _varint(buf, i)
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            break
    return None


def parse_seg(buf):
    """DanmakuSeg：顶层 repeated DanmakuElem(field 1)，内容在 field 7"""
    out = []
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        f, wt = key >> 3, key & 7
        if wt == 2:
            ln, i = _varint(buf, i)
            val = buf[i:i + ln]
            i += ln
            if f == 1:
                s = _msg_string(val, 7)
                if s:
                    out.append(s)
        elif wt == 0:
            _, i = _varint(buf, i)
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            break
    return out


def danmaku(vids, per_video=900, segments=2, delay=0.4):
    lines = []
    for aid, cid in vids:
        got = 0
        for seg in range(1, segments + 1):
            url = (API + "/x/v2/dm/web/seg.so?type=1&oid=%d&pid=%d&segment_index=%d"
                   % (cid, aid, seg))
            try:
                raw, _ctype, _final, _st = fetchx.http_get(
                    url, referer="https://www.bilibili.com/video/av%d" % aid)
            except fetchx.SourceRejected:
                break
            try:
                got += len(parse_seg(raw))
            except Exception:
                break
            lines.extend(parse_seg(raw)[:per_video])
            if len(parse_seg(raw)) < 10:
                break
            time.sleep(delay)
        time.sleep(delay)
    # 去重 + 长度过滤
    seen = set()
    out = []
    for s in lines:
        s = s.strip()
        s = clean_text(s)
        if 1 <= len(s) <= 40 and s not in seen:
            seen.add(s)
            out.append(s)
    if not out:
        raise fetchx.SourceRejected("弹幕一条都没拿到（412 或接口变更）")
    return out


# ---------------------------------------------------------------- 统一入口

def get(src):
    kind = src.get("type", "bilibili_popular")
    if kind == "bilibili_popular":
        lines, vids = popular(src.get("pages", 2), src.get("ps", 50))
        return lines, vids, "热门 %d 行 / %d 个视频" % (len(lines), len(vids))
    if kind == "bilibili_ranking":
        lines, vids = ranking(src.get("rid", 0))
        return lines, vids, "排行 %d 行 / %d 个视频" % (len(lines), len(vids))
    if kind in ("bilibili_comments", "bilibili_danmaku"):
        if src.get("vids"):
            vids = [(int(a), int(c)) for a, c in src["vids"]]
        else:
            _l, vids = popular(src.get("seed_pages", 1), src.get("seed_ps", 30))
        vids = vids[:src.get("videos", 12)]
        if kind == "bilibili_comments":
            lines = comments(vids, src.get("pages", 2), src.get("delay", 0.5))
            return lines, vids, "评论 %d 条 / %d 个视频" % (len(lines), len(vids))
        lines = danmaku(vids, src.get("per_video", 900),
                        src.get("segments", 2), src.get("delay", 0.4))
        return lines, vids, "弹幕 %d 条 / %d 个视频" % (len(lines), len(vids))
    raise fetchx.SourceRejected("未知的 B 站源类型：%s" % kind)
