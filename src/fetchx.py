#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetchx.py —— 带"坏页面体检"的抓取层
------------------------------------
词库最怕的不是抓不到，而是把 404 页、反爬提示页、空白 JSON、登录墙当成正文
灌进去。这里每一层都拦：

  1. HTTP 状态必须是 2xx（3xx 跟到底，4xx/5xx 直接判死）
  2. Content-Type 必须对得上（html / xml / json）
  3. 解压 gzip / deflate（服务端经常无视 identity）
  4. 错误页特征词命中 → 判死（"404"、"页面不存在"、"访问异常"、"请开启 JavaScript"…）
  5. 正文体检：汉字占比、句子数、重复度、长度
  6. JSON 必须 code==0 且 data 非空

任何一个不通过就抛 SourceRejected，上层跳过该源，绝不写进词库。
"""

import gzip
import json
import re
import zlib
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BAD_PATTERNS = [
    "404", "403 Forbidden", "页面不存在", "页面未找到", "网页不存在", "访问的页面已被删除",
    "访问异常", "请求过于频繁", "请稍后再试", "请开启 JavaScript", "请启用 JavaScript",
    "需要登录", "登录后查看", "人机验证", "安全验证", "滑动验证", "Access Denied",
    "Just a moment", "Enable JavaScript", "Not Found", "Service Unavailable",
    "该内容已被发布者删除", "账号异常", "操作过于频繁",
]


class SourceRejected(Exception):
    """这个源这次不可用，跳过，别污染词库"""


def _decompress(raw, encoding):
    enc = (encoding or "").lower()
    try:
        if "br" in enc:
            try:
                import brotli  # 可选依赖
                return brotli.decompress(raw)
            except ImportError:
                raise SourceRejected("服务端返回 brotli，环境没装 brotli 解不了")
        if "gzip" in enc or raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except SourceRejected:
        raise
    except Exception:
        pass
    return raw


def http_get(url, timeout=25, referer="", accept="*/*", max_bytes=8 << 20, proxy=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        **({"Referer": referer} if referer else {}),
    })
    handlers = ([urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
                if proxy else [])
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            status = r.status
            ctype = r.headers.get("Content-Type", "")
            enc = r.headers.get("Content-Encoding", "")
            raw = r.read(max_bytes)
            final = r.geturl()
    except urllib.error.HTTPError as e:
        raise SourceRejected("HTTP %s" % e.code)
    except Exception as e:  # noqa: BLE001
        raise SourceRejected("网络错误：%s" % e)

    if not (200 <= status < 300):
        raise SourceRejected("HTTP %s" % status)

    raw = _decompress(raw, enc)
    return raw, ctype, final, status


STRONG_BAD = [
    "页面不存在", "页面未找到", "网页不存在", "访问的页面已被删除", "访问异常",
    "请求过于频繁", "操作过于频繁", "请开启 JavaScript", "请启用 JavaScript",
    "需要登录", "登录后查看", "人机验证", "安全验证", "滑动验证", "Access Denied",
    "Just a moment", "Enable JavaScript", "Service Unavailable", "Forbidden",
    "该内容已被发布者删除", "账号异常", "内容已删除", "暂无内容", "加载失败",
]
WEAK_BAD = ["404", "Not Found", "找不到", "出错了", "服务器错误"]


def looks_like_error_page(text):
    """先看强特征（标题/提示语，命中即判死），再看弱特征（配合短页面）"""
    head = text[:1500]
    for p in STRONG_BAD:
        if p in head:
            return p
    for p in WEAK_BAD:
        if p in head and len(text) < 3000:
            return p
    return None


def cjk_ratio(text):
    if not text:
        return 0.0
    n = len(re.findall(r"[\u4e00-\u9fff]", text))
    return n / max(1, len(text))


def validate_text(text, min_chars=60, min_cjk_ratio=0.30, min_unique_ratio=0.10):
    """正文体检。返回统计 dict，不合格就抛"""
    text = (text or "").strip()
    if len(text) < min_chars:
        raise SourceRejected("正文太短（%d 字），多半是空页/JS 壳" % len(text))
    bad = looks_like_error_page(text)
    if bad:
        raise SourceRejected("命中错误页特征：%s" % bad)
    ratio = cjk_ratio(text)
    if ratio < min_cjk_ratio:
        raise SourceRejected("汉字占比只有 %.0f%%，不像中文正文" % (ratio * 100))
    uniq = len(set(text.replace("\n", ""))) / max(1, len(text.replace("\n", "")))
    if uniq < min_unique_ratio:
        raise SourceRejected("文本重复度过高（唯一字 %.0f%%）" % (uniq * 100))
    return {"chars": len(text), "cjk": round(ratio, 3)}


def get_json(url, timeout=25, referer="https://www.bilibili.com/", allow_empty=False):
    raw, ctype, final, _ = http_get(url, timeout=timeout, referer=referer,
                                    accept="application/json, text/plain, */*")
    txt = raw.decode("utf-8", "ignore").strip()
    if not txt:
        raise SourceRejected("空响应体")
    if txt[:1] not in "{[":
        raise SourceRejected("返回的不是 JSON")
    try:
        d = json.loads(txt)
    except json.JSONDecodeError:
        raise SourceRejected("JSON 解析失败")
    if isinstance(d, dict) and "code" in d:
        if d["code"] != 0:
            raise SourceRejected("接口 code=%s %s" % (d["code"], d.get("message", "")))
        if not allow_empty and not d.get("data"):
            raise SourceRejected("接口返回空 data")
    return d
