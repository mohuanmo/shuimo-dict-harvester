#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B 站文本源
----------
不打登录态、不碰视频流，只取"文字"：
    popular  热门列表（标题/简介/分区/UP主/推荐语）
    ranking  排行榜（同上）
    danmaku  热门视频弹幕（口语短句，组句词库最好的语料）
    view     单个视频的详细简介 + 分P 标题

注意：弹幕接口对海外 IP 偶尔 412，抓不到就抛异常，调度层会跳过该源。
"""

import html
import json
import re
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

API = "https://api.bilibili.com"


def _get(url, timeout=20, binary=False, referer="https://www.bilibili.com/"):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",     # 不要压缩，省得自己解
        "Connection": "close",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return (raw if binary else raw.decode("utf-8", "ignore")), r.status


def _api(path, timeout=20):
    txt, _ = _get(API + path, timeout=timeout)
    d = json.loads(txt)
    if d.get("code") != 0:
        raise RuntimeError("bilibili code=%s %s" % (d.get("code"), d.get("message")))
    return d.get("data") or {}


def _video_lines(v):
    """把一个视频 JSON 里所有能当语料的文字抽出来"""
    out = []
    for k in ("title", "desc", "tname", "dynamic"):
        val = v.get(k)
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
    owner = v.get("owner") or {}
    if owner.get("name"):
        out.append(str(owner["name"]))
    for t in (v.get("tags") or []):
        if isinstance(t, dict) and t.get("tag_name"):
            out.append(t["tag_name"])
    return out


def popular(pages=2, ps=50):
    """热门列表 → (文本行, [cid])"""
    lines, cids = [], []
    for pn in range(1, pages + 1):
        data = _api("/x/web-interface/popular?ps=%d&pn=%d" % (ps, pn))
        for v in data.get("list", []):
            lines += _video_lines(v)
            if v.get("cid"):
                cids.append(int(v["cid"]))
    return lines, cids


def ranking(rid=0):
    data = _api("/x/web-interface/ranking/v2?rid=%d&type=all" % rid)
    lines, cids = [], []
    for v in data.get("list", []):
        lines += _video_lines(v)
        if v.get("cid"):
            cids.append(int(v["cid"]))
    return lines, cids


def view(bvid):
    data = _api("/x/web-interface/view?bvid=" + bvid)
    lines = _video_lines(data)
    for p in data.get("pages", [])[:20]:
        if p.get("part"):
            lines.append(str(p["part"]))
    for st in data.get("staff", [])[:10]:
        if st.get("name"):
            lines.append(str(st["name"]))
    return lines


DM_TAG = re.compile(r"<d\b[^>]*>(.*?)</d>", re.S)


def danmaku(cids, per_video=800, delay=0.4):
    """弹幕 XML → 文本行。每条弹幕单独一行，交给断句层处理"""
    lines = []
    for i, cid in enumerate(cids):
        try:
            raw, _ = _get("https://comment.bilibili.com/%d.xml" % cid, binary=True)
        except Exception:
            time.sleep(delay)
            continue
        txt = raw.decode("utf-8", "ignore")
        got = DM_TAG.findall(txt)[:per_video]
        for g in got:
            g = html.unescape(g).strip()
            if g:
                lines.append(g)
        time.sleep(delay)
    if not lines:
        raise RuntimeError("一条弹幕都没拿到（可能是 412 / 接口变更）")
    return lines


def get(src):
    """统一入口：返回 (lines, 备注)"""
    kind = src.get("type", "bilibili_popular")
    if kind == "bilibili_popular":
        lines, cids = popular(src.get("pages", 2), src.get("ps", 50))
        return lines, "热门 %d 条视频 / %d 个 cid" % (len(lines), len(cids))
    if kind == "bilibili_ranking":
        lines, cids = ranking(src.get("rid", 0))
        return lines, "排行榜 %d 行" % len(lines)
    if kind == "bilibili_view":
        lines = []
        for b in src.get("bvids", [])[:20]:
            try:
                lines += view(b)
            except Exception as e:
                lines.append("")  # 单个失败不影响整体
        return lines, "view %d 行" % len(lines)
    if kind == "bilibili_danmaku":
        if src.get("cids"):
            cids = [int(c) for c in src["cids"]]
        else:
            _, cids = popular(src.get("pages", 1), 30)
        lines = danmaku(cids[:src.get("videos", 20)],
                        src.get("per_video", 800), src.get("delay", 0.4))
        return lines, "弹幕 %d 条 / %d 个视频" % (len(lines), min(len(cids), src.get("videos", 20)))
    raise ValueError("未知的 bilibili 源类型：%s" % kind)
