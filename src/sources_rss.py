#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS / Atom 源 —— 比抓首页靠谱得多：正文干净、结构稳定、没有 JS。

只依赖标准库。description 里夹的 HTML 会先剥标签。
"""

import re
import xml.etree.ElementTree as ET

import wordbank as wb


def _strip_html(s):
    if not s:
        return ""
    if "<" not in s:
        return s
    ex = wb.Extractor()
    ex.feed(s)
    return ex.text()


def _local(tag):
    return tag.split("}")[-1].lower()


def feed_text(raw):
    """返回 (文本行, 条目数)"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise RuntimeError("RSS 解析失败：%s" % e)

    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    lines = []
    for it in items[:80]:
        fields = {}
        for ch in it:
            name = _local(ch.tag)
            if name in ("title", "description", "summary", "content", "encoded"):
                txt = _strip_html("".join(ch.itertext()))
                txt = re.sub(r"\s+", " ", txt).strip()
                if txt:
                    fields[name] = fields.get(name, "") + txt
        for k in ("title", "summary", "description", "content", "encoded"):
            if fields.get(k):
                lines.append(fields[k])
    if not lines:
        raise RuntimeError("这个 feed 里没有可用的条目")
    return lines, len(items)
