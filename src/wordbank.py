#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组句词库生成器
--------------
输入一个网页（或本地 html / mhtml） -> 抓正文 -> 终端打印「词语 / 词性 / 频次 / 例句」和「句子库」。

用法：
    python3 wordbank.py https://example.com/a
    python3 wordbank.py ./page.mhtml -n 80 --min-freq 2
    python3 wordbank.py ./page.html -v            # 显示统计/计算过程（走 stderr）
    python3 wordbank.py ./page.html --why 薇帕     # 追一个词是怎么数出来的
    cat page.html | python3 wordbank.py -

可选依赖：pip install jieba（有就用，没有自动降级）
"""

import argparse
import os
import re
import sys
import time
import unicodedata
import urllib.request
from collections import Counter
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CJK = r"\u4e00-\u9fff"


def fmt(n):
    return f"{n:,}"


def pct(a, b):
    return f"{a * 100 / b:.1f}%" if b else "0.0%"


# ---------------------------------------------------------------- 文本工具

def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s, n):
    return s + " " * max(0, n - width(s))


class Trace:
    """统计过程的收集器，最后统一打到 stderr"""

    def __init__(self):
        self.rows = []

    def add(self, step, text):
        self.rows.append((step, text))

    def dump(self):
        if not self.rows:
            return
        w = max(width(s) for s, _ in self.rows) + 2
        out = ["", "═" * 78, "统 计 过 程", "═" * 78]
        for s, t in self.rows:
            lines = t.split("\n")
            out.append(f"{pad(s, w)}{lines[0]}")
            for extra in lines[1:]:
                out.append(" " * w + extra)
        print("\n".join(out), file=sys.stderr)


# ---------------------------------------------------------------- 抓取

def decode(raw, ctype):
    """按 Content-Type / meta charset / 常见编码依次尝试"""
    tries = []
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    if m:
        tries.append(m.group(1))
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
    if m:
        tries.append(m.group(1).decode("ascii", "ignore"))
    tries += ["utf-8", "gb18030"]
    for cs in tries:
        try:
            return raw.decode(cs, "strict"), cs
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "ignore"), "utf-8(忽略错误)"


def looks_like_mhtml(raw):
    """.mhtml / .mht（浏览器「网页，单个文件」另存）是 MIME 包，先剥壳"""
    head = raw[:4096].lstrip()
    return head[:5].lower() == b"from:" and b"mime-version" in raw[:4096].lower()


def mhtml_to_html(raw):
    """从 mhtml 里取出正文 HTML（取最大的 text/html 段）"""
    import email
    import email.policy
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    best = b""
    nparts = 0
    for part in msg.walk():
        nparts += 1
        if part.get_content_type() == "text/html":
            data = part.get_payload(decode=True) or b""
            if len(data) > len(best):
                best = data
    return best, nparts


def load_bytes(raw):
    """返回 (html, 说明)"""
    if looks_like_mhtml(raw):
        html, n = mhtml_to_html(raw)
        if not html:
            sys.exit("这个 mhtml 里没有 text/html 段，换个格式再试试")
        return html, f"mhtml 解包：{fmt(len(raw))} 字节 / {n} 个 MIME 段 → 内嵌 HTML {fmt(len(html))} 字节"
    return raw, f"原始文件 {fmt(len(raw))} 字节"


def fetch(url, proxy=None):
    """返回 (html_text, 真实地址, 抓取说明)"""
    if url == "-":
        raw = sys.stdin.buffer.read()
        html, note = load_bytes(raw)
        text, enc = decode(html, None)
        return text, "stdin", f"标准输入：{note.split('：')[0]}；编码 {enc}"
    if not re.match(r"^https?://", url, re.I):
        with open(url, "rb") as f:
            raw = f.read()
        html, note = load_bytes(raw)
        text, enc = decode(html, None)
        return text, os.path.abspath(url), f"{note}；解码 {enc}"

    # 不给 --proxy 时沿用环境变量 https_proxy / http_proxy
    handlers = ([urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
                if proxy else [])
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",   # 不要压缩，省得解 gzip
    })
    with opener.open(req, timeout=25) as r:
        raw = r.read()
        ctype = r.headers.get("Content-Type", "")
        status, real = r.status, r.geturl()
    if looks_like_mhtml(raw):
        raw, _ = mhtml_to_html(raw)
    text, enc = decode(raw, ctype)
    return text, real, f"HTTP {status} {fmt(len(raw))} 字节 → {enc}"


class Extractor(HTMLParser):
    """丢掉脚本/导航/页脚，只留正文，块级标签处断行"""
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside",
            "form", "svg", "iframe", "button", "select", "textarea", "template"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5",
             "h6", "tr", "td", "th", "section", "article", "blockquote", "pre",
             "dd", "dt", "figcaption", "table", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.buf = []
        self.title = ""
        self.in_title = False
        self.dropped_tags = Counter()
        self.n_tags = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self.n_tags += 1
        if tag in self.SKIP:
            self.skip += 1
            self.dropped_tags[tag] += 1
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = True
        if tag in self.BLOCK:
            self.buf.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "title":
            self.in_title = False
        if self.skip:
            return
        if tag in self.BLOCK:
            self.buf.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title += data
        elif data.strip():
            self.buf.append(data)

    def text(self):
        t = "".join(self.buf).replace("\u00a0", " ").replace("\u3000", " ")
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r"\n\s*\n+", "\n", t)
        return t.strip()


# ---------------------------------------------------------------- 断句

SENT_RE = re.compile(r".+?(?:[。！？!?]+|…+|\.(?=\s|$)|$)", re.S)
MIN_SENT = 8


def split_sentences(text):
    """返回 (句子列表, 统计)"""
    sents = []
    cand = short = nocjk = 0
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for s in SENT_RE.findall(line):
            cand += 1
            s = s.strip(" \t。．.,，、；;：:")
            if len(s) < MIN_SENT:
                short += 1
                continue
            if not re.search(rf"[A-Za-z{CJK}]", s):
                nocjk += 1
                continue
            sents.append(s)
    lens = [len(s) for s in sents]
    stat = {
        "cand": cand, "short": short, "nocjk": nocjk,
        "n": len(sents),
        "avg": (sum(lens) / len(lens)) if lens else 0,
        "max": max(lens) if lens else 0,
        "min": min(lens) if lens else 0,
    }
    return sents, stat


# ---------------------------------------------------------------- 分词

STOP = set("""
的 了 和 是 在 我 有 就 不 人 都 一 上 也 很 到 说 要 去 你 会 着 没 没有 看 好
这 那 这个 那个 我们 你们 他们 它们 自己 什么 怎么 可以 因为 所以 但是 但 如果
已经 还是 这样 那样 时候 现在 这里 那里 这么 那么 只是 而且 以及 不过 然后 之后
之前 一些 这些 那些 通过 关于 对于 由于 根据 其中 以上 以下 方面 问题 进行 作为
不是 就是 可能 应该 需要 觉得 知道 起来 出来 过来 下来 一下 一点 一直 一定 一般
一样 这种 那种 东西 事情 情况 方式 一个 并且 或者 虽然 然而 因此 只是 还要 不能
没有 已经 只是 于是 同时 其实 这些 那些 我们 他们 她们 大家 各种 呢 吧 啊 吗
the a an and or but if while of to in on at for with from by as is are was were be been
being it its this that these those there here he she they we you i his her their our your
not no do does did have has had will would can could should may might must than then so
such very more most other some any what which who whom when where why how all both each
""".split())

JUNK_EDGE = tuple("的了是在和与及或就也都而之其把被给对为以于")

REASON = {
    "punct": "纯符号/空白",
    "stop": "停用词",
    "len": "长度<2（单字）",
    "digit": "纯数字/符号",
    "ascii": "英文过短(<3)",
    "edge": "虚词开头/结尾",
}


def check_word(w):
    """返回 None 表示保留，否则返回丢弃原因"""
    if not re.search(rf"[\w{CJK}]", w):
        return "punct"
    if w in STOP:
        return "stop"
    if len(w) < 2:
        return "len"
    if re.fullmatch(r"[\d\W_]+", w):
        return "digit"
    if re.fullmatch(r"[A-Za-z']+", w) and len(w) < 3:
        return "ascii"
    if w[0] in JUNK_EDGE or w[-1] in JUNK_EDGE:
        return "edge"
    return None


POS_MAP = {
    "n": "名词", "nr": "人名", "ns": "地名", "nt": "机构", "nz": "专名",
    "v": "动词", "vn": "动名词", "vd": "副动词", "a": "形容词", "an": "名形词",
    "ad": "副形词", "d": "副词", "m": "数词", "q": "量词", "r": "代词",
    "p": "介词", "c": "连词", "u": "助词", "t": "时间词", "f": "方位词",
    "s": "处所词", "y": "语气词", "e": "叹词", "o": "拟声词", "i": "成语",
    "l": "惯用语", "j": "简称", "eng": "英文", "x": "其他", "": "未标注",
}


def pos_cn(flag):
    if flag in POS_MAP:
        return POS_MAP[flag]
    for k in ("n", "v", "a", "d", "m", "t", "r", "q", "u"):
        if flag.startswith(k):
            return POS_MAP[k]
    return "其他"


CJK_RUN = re.compile(rf"[{CJK}]+")
LATIN = re.compile(r"[A-Za-z][A-Za-z'-]+")
CJK1 = re.compile(rf"^[{CJK}]$")


def make_vocab(text, min_freq=3):
    """兜底分词用：高频 2~4 字片段当词表（新词发现思路）"""
    cnt = Counter()
    for run in CJK_RUN.findall(text):
        for n in (4, 3, 2):
            for i in range(len(run) - n + 1):
                cnt[run[i:i + n]] += 1
    vocab = {g for g, c in cnt.items() if c >= min_freq}
    for g in list(vocab):
        for n in (3, 4):
            if len(g) < n:
                continue
            for i in range(len(g) - n + 1):
                sub = g[i:i + n]
                if sub != g and sub in vocab and cnt[sub] == cnt[g]:
                    vocab.discard(sub)
    return vocab, cnt


def simple_segment(text, vocab):
    """最长匹配，返回 [(词, 词性)]"""
    out = []
    for run in CJK_RUN.findall(text):
        i = 0
        while i < len(run):
            for n in (4, 3, 2):
                seg = run[i:i + n]
                if len(seg) == n and seg in vocab:
                    out.append((seg, ""))
                    i += n
                    break
            else:
                out.append((run[i], ""))
                i += 1
    for w in LATIN.findall(text):
        out.append((w.lower(), "eng"))
    return out


# 单字词性是这些开头的一律不参与合并（助词/介词/连词/语气/标点…）
MERGE_BLOCK = set("xwyeopc")
# 候选词里出现这些字就否决（防止把「他的」「的薇帕」拼成词）
MERGE_BAD_CHARS = set("的了吗呢吧啊是在和与或就也都而之其把被给对为以于着过得地我你他她它这那"
                      "上下中里个有不还又也很更最")


def merge_unknowns(tokens, min_freq=3):
    """jieba 没有的人名/生造词会被切成单字，把反复出现的连续单字重新合并成词"""
    def ok(i):
        w, f = tokens[i]
        return (len(w) == 1 and CJK1.match(w) is not None
                and (f[:1] not in MERGE_BLOCK))

    segs = []
    i = 0
    while i < len(tokens):
        if ok(i):
            j = i
            while j < len(tokens) and ok(j):
                j += 1
            if j - i >= 2:
                segs.append((i, j))
            i = j
        else:
            i += 1
    info = {"segs": len(segs), "cand": 0, "merged": []}
    if not segs:
        return tokens, info

    cnt = Counter()
    for a, b in segs:
        s = "".join(tokens[k][0] for k in range(a, b))
        for n in (2, 3, 4):
            for p in range(len(s) - n + 1):
                cnt[s[p:p + n]] += 1
    info["cand"] = len(cnt)

    # 只接受「不像虚词粘连」的片段：不以的/了/是…开头结尾
    vocab = {g for g, c in cnt.items()
             if c >= min_freq and not (set(g) & MERGE_BAD_CHARS)}
    for g in list(vocab):
        for n in (3, 4):
            if len(g) < n:
                continue
            for p in range(len(g) - n + 1):
                sub = g[p:p + n]
                if sub != g and sub in vocab and cnt[sub] == cnt[g]:
                    vocab.discard(sub)
    if not vocab:
        return tokens, info

    maxlen = max(len(g) for g in vocab)
    merge = {}
    for a, b in segs:
        s = "".join(tokens[k][0] for k in range(a, b))
        p = 0
        while p < len(s):
            for n in range(min(maxlen, len(s) - p), 1, -1):
                if s[p:p + n] in vocab:
                    merge[a + p] = n
                    p += n
                    break
            else:
                p += 1

    out, i = [], 0
    while i < len(tokens):
        n = merge.get(i)
        if n:
            w = "".join(tokens[k][0] for k in range(i, i + n))
            out.append((w, ""))
            info["merged"].append(w)
            i += n
        else:
            out.append(tokens[i])
            i += 1
    return out, info


def tokenize(text):
    """优先 jieba，失败走兜底。返回 (tokens, 引擎名, 额外信息)"""
    try:
        import logging
        import jieba
        import jieba.posseg as pseg
        jieba.setLogLevel(logging.ERROR)
        raw = [(w, f) for w, f in pseg.cut(text)]
        toks, minfo = merge_unknowns(raw)
        return toks, "jieba", {"raw": len(raw), "merge": minfo}
    except ImportError:
        vocab, cnt = make_vocab(text)
        toks = simple_segment(text, vocab)
        return toks, "内置兜底（pip install jieba 更准）", {
            "raw": len(toks), "vocab": len(vocab), "ngrams": len(cnt),
            "merge": {"segs": 0, "cand": 0, "merged": []}}


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="抓取网页并生成组句词库")
    ap.add_argument("url", help="网页地址 / 本地 html、mhtml 路径 / -（标准输入）")
    ap.add_argument("-n", "--top", type=int, default=60, help="最多显示多少词条（默认 60）")
    ap.add_argument("--min-freq", type=int, default=1, help="最低出现次数（默认 1）")
    ap.add_argument("--sent", type=int, default=20, help="句子库条数（默认 20）")
    ap.add_argument("--tsv", action="store_true", help="纯 Tab 分隔，便于导入表格/Anki")
    ap.add_argument("--proxy", help="代理，如 http://127.0.0.1:7890（也可用环境变量 https_proxy）")
    ap.add_argument("-v", "--verbose", action="store_true", help="输出统计/计算过程（走 stderr）")
    ap.add_argument("--why", metavar="WORD", help="追踪某个词的每一次出现")
    a = ap.parse_args()

    tr = Trace()
    t0 = time.time()

    # [1] 抓取
    try:
        html, real_url, fetch_note = fetch(a.url, a.proxy)
    except Exception as e:
        sys.exit(f"""抓取失败：{e}

常见原因与对策：
  1. 目标站在你这条线路上被拦（TLS 握手被 RST）—— 需要代理：
       export https_proxy=http://127.0.0.1:7890
     或 python3 wordbank.py <url> --proxy http://127.0.0.1:7890
  2. 站点有反爬/需登录 —— 用浏览器打开页面，Ctrl+S 存成
     「网页，单个文件 (*.mhtml)」，再：
       python3 wordbank.py ./xxx.mhtml
  3. 页面是 JS 动态渲染 —— 同样用浏览器另存后跑（脚本能读 .mhtml）""")
    tr.add("[1] 抓取", fetch_note)

    # [2] 正文抽取
    ex = Extractor()
    ex.feed(html)
    text = ex.text()
    title = re.sub(r"\s+", " ", ex.title).strip()
    if len(text) < 50:
        sys.exit("正文太短，可能是 JS 动态渲染的页面（试试用浏览器另存为 html 再传入）")
    # 标题若顶在正文最前面才去掉（避免把正文里的同名字/词误删）
    if title and text.startswith(title):
        text = text[len(title):].lstrip(" \n-_·—|")
    lines = [l for l in text.split("\n") if l.strip()]
    skip_note = "、".join(f"{k}×{v}" for k, v in ex.dropped_tags.most_common(4))
    tr.add("[2] 正文抽取",
           f"{fmt(ex.n_tags)} 个标签 → 剔除 {skip_note or '无'}\n"
           f"{'':2}纯文本 {fmt(len(text))} 字 / {fmt(len(lines))} 段，"
           f"平均 {len(text) / max(1, len(lines)):.1f} 字/段")

    # [3] 断句
    sents, sst = split_sentences(text)
    tr.add("[3] 断句",
           f"候选 {fmt(sst['cand'])} 句 → 合格 {fmt(sst['n'])} 句"
           f"（丢弃 {fmt(sst['short'] + sst['nocjk'])}：<{MIN_SENT}字 {fmt(sst['short'])}、无文字 {fmt(sst['nocjk'])}）\n"
           f"{'':2}句长 平均 {sst['avg']:.1f} 字，最长 {sst['max']}，最短 {sst['min']}")

    # [4] 分词
    tokens, engine, tinfo = tokenize(text)
    n_single = sum(1 for w, f in tokens if len(w) == 1 and CJK1.match(w))
    n_punct = sum(1 for w, f in tokens if not re.search(rf"[\w{CJK}]", w))
    n_multi = len(tokens) - n_single - n_punct
    tr.add("[4] 分词",
           f"{engine}：原始 token {fmt(tinfo['raw'])} 个\n"
           f"{'':2}其中 多字词 {fmt(n_multi)} / 汉字单字 {fmt(n_single)} / 标点空白 {fmt(n_punct)}")
    mi = tinfo.get("merge") or {}
    if mi.get("merged"):
        top = "、".join(f"{w}×{c}" for w, c in Counter(mi["merged"]).most_common(5))
        tr.add("[4b] 未知词合并",
               f"连续单字段 {fmt(mi['segs'])} 处 → 候选片段 {fmt(mi['cand'])} 个 → "
               f"采纳 {fmt(len(mi['merged']))} 个\n{'':2}最多：{top}")
    elif "vocab" in tinfo:
        tr.add("[4b] 兜底词表", f"n-gram 候选 {fmt(tinfo['ngrams'])} → 进词表 {fmt(tinfo['vocab'])}")

    # [5] 过滤 + [6] 计数
    reasons = Counter()
    dropped = Counter()
    freq = Counter()
    pos_of = {}
    pos_dist = Counter()
    for w, f in tokens:
        r = check_word(w)
        if r:
            reasons[r] += 1
            if r != "punct":
                dropped[w] += 1
            continue
        freq[w] += 1
        if w not in pos_of:
            pos_of[w] = pos_cn(f) if f else "—"
        pos_dist[pos_of[w]] += 1

    kept = sum(freq.values())
    tr.add("[5] 过滤",
           f"{fmt(len(tokens))} token → 保留 {fmt(kept)}（{pct(kept, len(tokens))}）\n"
           f"{'':2}" + " | ".join(f"{REASON[k]} −{fmt(v)}" for k, v in reasons.most_common()) + "\n"
           f"{'':2}丢弃最多：" + "、".join(f"{w}×{c}" for w, c in dropped.most_common(8)) + "\n"
           f"{'':2}校验：{fmt(sum(reasons.values()))} 丢弃 + {fmt(kept)} 保留 = {fmt(len(tokens))}")

    uniq = len(freq)
    dist = Counter()
    for c in freq.values():
        dist["×1" if c == 1 else "×2" if c == 2 else "×3-5" if c <= 5 else "≥6"] += 1
    tr.add("[6] 去重计数",
           f"唯一词 {fmt(uniq)} 个，总频次 {fmt(kept)}，平均 {kept / max(1, uniq):.2f} 次/词\n"
           f"{'':2}" + " | ".join(f"{k} {fmt(dist[k])} ({pct(dist[k], uniq)})"
                                  for k in ("×1", "×2", "×3-5", "≥6")) + "\n"
           f"{'':2}词性分布：" + " | ".join(f"{k} {fmt(v)}" for k, v in pos_dist.most_common(6)))

    rows = [(w, c) for w, c in freq.most_common() if c >= a.min_freq][:a.top]
    tr.add("[7] 排序输出",
           f"按频次降序 → 全部 {fmt(uniq)} 词，min-freq={a.min_freq} 过滤后 "
           f"{fmt(len([1 for c in freq.values() if c >= a.min_freq]))} 词，取前 {len(rows)}\n"
           f"{'':2}句子库取前 {min(a.sent, len(sents))} 句；总耗时 {time.time() - t0:.2f}s")

    # ---------------- 输出
    out = []
    add = out.append
    add(f"# 组句词库  {time.strftime('%Y-%m-%d %H:%M')}")
    add(f"# 来源：{real_url}")
    if title:
        add(f"# 标题：{title}")
    add(f"# 正文：{len(text)} 字 / {len(sents)} 句   分词：{engine}")
    add(f"# 词条：{uniq} 个（显示 {len(rows)}）")
    add("")

    def example(w):
        for s in sents:
            if w in s:
                return s if len(s) <= 60 else s[:59] + "…"
        return ""

    if a.tsv:
        add("词语\t词性\t频次\t占比\t例句")
        for w, c in rows:
            add(f"{w}\t{pos_of.get(w, '—')}\t{c}\t{pct(c, kept)}\t{example(w)}")
    else:
        add(f"{pad('词语', 16)}{pad('词性', 8)}{pad('频次', 6)}{pad('占比', 8)}例句")
        add("-" * 88)
        for w, c in rows:
            add(f"{pad(w, 16)}{pad(pos_of.get(w, '—'), 8)}{pad(str(c), 6)}"
                f"{pad(pct(c, kept), 8)}{example(w)}")

    add("")
    add(f"# ===== 句子库（前 {min(a.sent, len(sents))} 句）=====")
    for i, s in enumerate(sents[:a.sent], 1):
        add(f"S{i:03d}  {s}")

    print("\n".join(out))

    # ---------------- 统计过程 / 单词追踪
    if a.verbose:
        tr.dump()
    if a.why:
        w = a.why
        hits = []
        for i, s in enumerate(sents, 1):
            p = s.find(w)
            n = s.count(w)
            while p != -1:
                hits.append((i, p, s[max(0, p - 12):p + len(w) + 12]))
                p = s.find(w, p + len(w))
        print(f"\n===== 追踪「{w}」=====", file=sys.stderr)
        print(f"分词结果里的词条频次：{freq.get(w, 0)}", file=sys.stderr)
        print(f"在句子库中实际出现：{len(hits)} 次（{len({h[0] for h in hits})} 句）", file=sys.stderr)
        for i, p, ctx in hits[:40]:
            print(f"  S{i:03d} 第{p}字  …{ctx}…", file=sys.stderr)
        if len(hits) > 40:
            print(f"  …… 其余 {len(hits) - 40} 处省略", file=sys.stderr)


if __name__ == "__main__":
    main()
