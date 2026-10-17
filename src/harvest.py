#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
harvest.py —— 水墨词库每日增量构建

流水线：
    多源抓取（html / rss / bilibili）→ 正文抽取 → 断句 → 分词
    → 词频 + bigram 共现 → 过滤 → 单源封顶 → 历史状态合并（衰减/淘汰）
    → 导出：① 组句词库（词条/拼音/词频） ② bigram 词对 ③ 通用词库

用法：
    python3 src/harvest.py                 # 按 config/sources.yaml 跑
    python3 src/harvest.py --only 弹幕      # 只跑匹配的源
    python3 src/harvest.py --url xxx.html  # 临时源
    python3 src/harvest.py --reset         # 丢掉历史，从零重建
    python3 src/harvest.py --dry-run       # 只统计不落盘
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import wordbank as wb          # 抓取 / 抽取 / 断句 / 分词
import sources_bili            # B 站
import sources_rss             # RSS / Atom
import fetchx                  # 带体检的 HTTP 层

try:
    import yaml
except ImportError:
    yaml = None

HAN = re.compile(r"^[\u4e00-\u9fff]+$")
CJK = r"\u4e00-\u9fff"


# ---------------------------------------------------------------- 配置

DEFAULTS = {
    "output": {
        "dir": "data/dict",
        "formats": [
            {"type": "juzi", "file": "shuimo_juzi.dict",
             "min_freq": 2, "min_len": 2, "max_len": 6, "pinyin_sep": ""},
            {"type": "bigram", "file": "shuimo_bigram.txt",
             "min_count": 2, "max_word_len": 4, "max_rows": 200000},
            {"type": "raw", "file": "shuimo.dict",
             "template": "{word}\t{freq}\t{pos}\t{cls}",
             "min_freq": 2, "split_by_class": True},
        ],
        "encoding": "utf-8",
    },
    "scoring": {
        "cap_per_source": 300,
        "default_weight": 1.0,
        "half_life_days": 90,
        "max_age_days": 400,
        "drop_below": 0.8,
    },
    "filter": {"only_chinese": True, "min_len": 2, "max_len": 12},
    "bigram": {"max_rows": 300000, "min_count_keep": 1},
    "fetch": {"delay": 1.5, "timeout": 25, "retries": 2,
              "respect_robots": True, "save_raw": True},
    "sources": [],
}


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if path.endswith((".yaml", ".yml")):
        if yaml is None:
            sys.exit("配置是 yaml，但没装 PyYAML：pip install pyyaml")
        data = yaml.safe_load(raw) or {}
    else:
        data = json.loads(raw)
    return deep_merge(DEFAULTS, data)


# ---------------------------------------------------------------- 抓取

def robots_allowed(url, ua):
    from urllib.parse import urlparse
    from urllib import robotparser
    p = urlparse(url)
    if not p.scheme.startswith("http"):
        return True
    rp = robotparser.RobotFileParser()
    rp.set_url("%s://%s/robots.txt" % (p.scheme, p.netloc))
    try:
        rp.read()
        return rp.can_fetch(ua, url)
    except Exception:
        return True


def _html_text(url, fcfg, proxy=None):
    if not re.match(r"^https?://", url, re.I):
        # 本地文件：调试用（也支持 .mhtml）
        with open(url, "rb") as f:
            raw = f.read()
        if raw[:5].lower() == b"from:":
            from email import message_from_bytes
            from email.policy import compat32
            msg = message_from_bytes(raw, policy=compat32)
            best = b""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    d = part.get_payload(decode=True) or b""
                    if len(d) > len(best):
                        best = d
            raw = best or raw
        ctype, final, status = "", os.path.abspath(url), 200
    else:
        raw, ctype, final, status = fetchx.http_get(
            url, timeout=fcfg["timeout"], proxy=proxy,
            accept="text/html,application/xhtml+xml")
    text, enc = wb.decode(raw, ctype)
    ex = wb.Extractor()
    ex.feed(text)
    t = ex.text()
    title = re.sub(r"\s+", " ", ex.title).strip()
    if title and t.startswith(title):
        t = t[len(title):].lstrip(" \n-_·—|")
    return t, final, "HTTP %s %s字节 %s" % (status, format(len(raw), ","), enc)


def source_text(src, cfg, proxy=None):
    """按 type 取纯文本，返回 (文本, 真实地址, 备注)。抓不到就抛"""
    kind = src.get("type", "html")
    fcfg = cfg["fetch"]

    if kind.startswith("bilibili"):
        lines, _vids, note = sources_bili.get(src)
        return "\n".join(lines), src.get("url", "bilibili://" + kind), note

    if fcfg.get("respect_robots") and not robots_allowed(src["url"], wb.UA):
        raise fetchx.SourceRejected("robots.txt 不允许抓取")

    if kind == "rss":
        raw, ctype, final, status = fetchx.http_get(
            src["url"], timeout=fcfg["timeout"], proxy=proxy,
            accept="application/rss+xml,application/xml,text/xml,*/*")
        text, n = sources_rss.feed_text(raw)
        return "\n".join(text), final, "feed %d 条" % n

    return _html_text(src["url"], fcfg, proxy)


# ---------------------------------------------------------------- 单源处理

BOUND = re.compile(r"^[\W_]+$")


def iter_bigrams(tokens):
    """同一句内、过滤掉虚词后的相邻词对；标点处断开"""
    prev = None
    for w, _flag in tokens:
        if not w or not w.strip():
            continue
        if BOUND.match(w):
            prev = None
            continue
        if wb.check_word(w):
            continue
        if not (1 <= len(w) <= 4 and HAN.match(w)):
            prev = None
            continue
        if prev:
            yield prev, w
        prev = w


def process_source(src, cfg, proxy=None):
    name = src.get("name") or src.get("url")
    text, real_url, note = source_text(src, cfg, proxy)
    # 这一层是关键：404/登录墙/空白 JSON 在这里被拦下，绝不进词库
    try:
        vinfo = fetchx.validate_text(text,
                                     min_chars=src.get("min_chars", 60),
                                     min_cjk_ratio=src.get("min_cjk", 0.30),
                                     min_unique_ratio=src.get("min_unique", 0.10))
    except fetchx.SourceRejected as e:
        raise fetchx.SourceRejected("%s ｜ 样本：%s" % (e, text[:70].replace("\n", " ")))
    text = text.strip()

    old_min = wb.MIN_SENT
    wb.MIN_SENT = int(src.get("min_sent", old_min))
    try:
        sents, sst = wb.split_sentences(text) if src.get("sentences", True) else ([], {"n": 0})
    finally:
        wb.MIN_SENT = old_min

    tokens, engine, _tinfo = wb.tokenize(text)

    fcfg2 = cfg.get("filter", {})
    only_cn = fcfg2.get("only_chinese", True)
    min_len = int(fcfg2.get("min_len", 2))
    max_len = int(fcfg2.get("max_len", 12))

    freq, pos_of, dropped = Counter(), {}, 0
    for w, flag in tokens:
        if wb.check_word(w):
            dropped += 1
            continue
        # 只收中文：英文 / 数字 / 假名 / 符号一律不要
        if only_cn and not HAN.match(w):
            dropped += 1
            continue
        if not (min_len <= len(w) <= max_len):
            dropped += 1
            continue
        freq[w] += 1
        if w not in pos_of:
            pos_of[w] = wb.pos_cn(flag)

    bg = Counter()
    for a, b in iter_bigrams(tokens):
        bg[(a, b)] += 1

    if cfg["fetch"].get("save_raw"):
        try:
            raw_dir = os.path.join(ROOT, "data", "raw")
            os.makedirs(raw_dir, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", str(name))[:40]
            with open(os.path.join(raw_dir, "%s-%s.txt" % (stamp, safe)), "w",
                      encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    stat = {"name": name, "url": real_url, "ok": True, "type": src.get("type", "html"),
            "chars": len(text), "sentences": sst.get("n", 0),
            "tokens": len(tokens), "kept": sum(freq.values()),
            "dropped": dropped, "unique": len(freq), "cjk": vinfo["cjk"],
            "bigrams": len(bg), "engine": engine, "note": note,
            "top": freq.most_common(8)}
    return freq, pos_of, sents, bg, stat


def score_source(freq, weight, cap):
    return {w: min(c * weight, cap) for w, c in freq.items()}


# ---------------------------------------------------------------- 分类

CLASS_RULES = {
    "科技数码": ["手机", "芯片", "电脑", "软件", "算法", "模型", "数据", "网络", "系统",
                "程序", "代码", "服务器", "云", "人工智能", "机器学习", "接口", "显卡",
                "处理器", "屏幕", "电池", "相机", "应用", "版本", "开源", "编译"],
    "财经商业": ["市场", "公司", "投资", "股票", "基金", "银行", "贷款", "经济", "营收",
                "利润", "成本", "价格", "消费", "交易", "融资", "上市", "税", "工资",
                "合同", "订单", "供应", "客户", "品牌", "销售"],
    "医疗健康": ["医院", "医生", "患者", "治疗", "疾病", "症状", "药物", "手术", "检查",
                "感染", "疫苗", "体质", "营养", "康复", "门诊", "病房", "临床", "免疫"],
    "体育竞技": ["比赛", "球队", "球员", "赛季", "联赛", "冠军", "教练", "进球", "比分",
                "训练", "奥运", "足球", "篮球", "田径", "游泳", "网球", "运动员"],
    "文教文史": ["学校", "学生", "老师", "课程", "考试", "论文", "教授", "研究", "文学",
                "小说", "诗歌", "历史", "文化", "博物馆", "教育", "专业", "毕业", "图书馆"],
    "时政社会": ["政府", "政策", "部门", "会议", "代表", "法律", "法院", "公安", "社区",
                "居民", "群众", "改革", "领导", "干部", "基层", "民生", "就业", "保障"],
    "生活日常": ["吃饭", "睡觉", "天气", "衣服", "房子", "家人", "孩子", "朋友", "周末",
                "出门", "早上", "晚上", "超市", "快递", "装修", "房租", "地铁", "外卖"],
    "饮食烹饪": ["食材", "做法", "锅", "火", "油", "盐", "糖", "酱", "蒸", "炒", "煮",
                "烤", "味道", "口感", "餐厅", "菜谱", "早茶", "面条"],
    "自然地理": ["山", "河", "湖", "海", "森林", "气候", "温度", "降雨", "台风", "地震",
                "生态", "物种", "植物", "动物", "湿地", "高原", "沙漠", "海洋"],
    "网络流行": ["网友", "热搜", "视频", "直播", "评论区", "粉丝", "点赞", "转发", "破防",
                "整活", "绝了", "弹幕", "打卡", "上分", "氪金", "up主"],
}

POS_CLASS = {
    "人名": "人名", "地名": "地名", "机构": "机构", "专名": "专名",
    "成语": "成语", "惯用语": "惯用语", "简称": "简称",
    "英文": "英文", "拟声词": "拟声词", "叹词": "语气",
}


def classify(word, pos):
    if pos in POS_CLASS:
        return POS_CLASS[pos]
    hits = Counter()
    for cls, kws in CLASS_RULES.items():
        for k in kws:
            if k in word:
                hits[cls] += 1
    if hits:
        return hits.most_common(1)[0][0]
    return {"名词": "通用名词", "动词": "动词", "形容词": "形容词", "副词": "副词",
            "数词": "数词", "量词": "量词", "代词": "代词", "时间词": "时间",
            "未标注": "未标注", "—": "未标注"}.get(pos, "其他")


# ---------------------------------------------------------------- 状态

def load_state(path):
    if not os.path.exists(path):
        return {"last_run": None, "words": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def merge_state(state, today_scores, pos_of, src_of, cfg, today):
    sc = cfg["scoring"]
    words = state.get("words", {})
    last = state.get("last_run")
    delta = 1
    if last:
        try:
            delta = max(1, (dt.date.fromisoformat(today) - dt.date.fromisoformat(last)).days)
        except ValueError:
            delta = 1

    hl = sc.get("half_life_days") or 0
    if hl > 0:
        f = 0.5 ** (delta / hl)
        for rec in words.values():
            rec["f"] *= f

    for w, s in today_scores.items():
        rec = words.get(w)
        if rec is None:
            words[w] = {"f": s, "p": pos_of.get(w, "—"), "src": src_of.get(w, []),
                        "first": today, "last": today, "hits": 1}
        else:
            rec["f"] += s
            rec["last"] = today
            rec["hits"] = rec.get("hits", 1) + 1
            if rec.get("p") in (None, "—", "未标注"):
                rec["p"] = pos_of.get(w, rec.get("p", "—"))
            for s_ in src_of.get(w, []):
                if s_ not in rec.setdefault("src", []):
                    rec["src"].append(s_)

    max_age = sc.get("max_age_days") or 0
    floor = sc.get("drop_below") or 0
    drop = []
    for w, rec in words.items():
        if max_age:
            try:
                age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(rec["last"])).days
            except ValueError:
                age = 0
            if age > max_age:
                drop.append(w)
                continue
        if rec["f"] < floor:
            drop.append(w)
    for w in drop:
        words.pop(w, None)

    state["words"] = words
    state["last_run"] = today
    return state, drop


def load_bigram(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return {(k.split(" ", 1)[0], k.split(" ", 1)[1]): v for k, v in d.items()}


def merge_bigram(old, counts, cfg):
    """bigram 用真实整数共现次数，不做衰减（要的是"语料统计值"）"""
    old = dict(old)
    for k, v in counts.items():
        old[k] = old.get(k, 0) + v
    cap = cfg["bigram"].get("max_rows", 300000)
    if len(old) > cap:
        keep = sorted(old.items(), key=lambda kv: -kv[1])[:cap]
        old = dict(keep)
    return old


# ---------------------------------------------------------------- 导出

def _pinyin(w, sep):
    from pypinyin import lazy_pinyin, Style
    return sep.join(lazy_pinyin(w, style=Style.NORMAL, errors=lambda x: [""] * len(x)))


def export_juzi(rows, path, fmt, enc):
    min_freq = fmt.get("min_freq", 2)
    min_len = fmt.get("min_len", 2)
    max_len = fmt.get("max_len", 6)
    sep = fmt.get("pinyin_sep", "")
    n = 0
    with open(path, "w", encoding=enc, newline="\n") as f:
        for w, freq, _pos, _cls in rows:
            if freq < min_freq:
                continue
            if not HAN.match(w) or not (min_len <= len(w) <= max_len):
                continue
            p = _pinyin(w, sep)
            if not p:
                continue
            f.write("%s\t%s\t%d\n" % (w, p, freq))
            n += 1
    return n


def export_bigram(bg, path, fmt, enc):
    min_count = fmt.get("min_count", 2)
    maxlen = fmt.get("max_word_len", 4)
    max_rows = fmt.get("max_rows", 200000)
    items = []
    for (a, b), c in bg.items():
        if c < min_count:
            continue
        if not (1 <= len(a) <= maxlen and 1 <= len(b) <= maxlen):
            continue
        if not (HAN.match(a) and HAN.match(b)):
            continue
        items.append((a, b, c))
    items.sort(key=lambda x: -x[2])
    items = items[:max_rows]
    with open(path, "w", encoding=enc, newline="\n") as f:
        for a, b, c in items:
            f.write("%s %s %d\n" % (a, b, c))
    return len(items)


def export_raw(rows, out_dir, fmt, enc, today):
    os.makedirs(out_dir, exist_ok=True)
    tmpl = fmt.get("template", "{word}\t{freq}\t{pos}\t{cls}")
    min_freq = fmt.get("min_freq", 2)
    path = os.path.join(out_dir, fmt.get("file", "shuimo.dict"))
    sel = [r for r in rows if r[1] >= min_freq]
    per_class = Counter(r[3] for r in sel)
    with open(path, "w", encoding=enc, newline="\n") as f:
        f.write("# 水墨词库  %s  共 %d 词\n" % (today, len(sel)))
        for r in sel:
            f.write(tmpl.replace("{word}", r[0]).replace("{freq}", str(r[1]))
                    .replace("{pos}", r[2]).replace("{cls}", r[3]) + "\n")
    if fmt.get("split_by_class"):
        cdir = os.path.join(out_dir, "classes")
        os.makedirs(cdir, exist_ok=True)
        bucket = {}
        for r in sel:
            bucket.setdefault(r[3], []).append(r)
        for cls, rs in bucket.items():
            safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", cls)
            with open(os.path.join(cdir, safe + ".txt"), "w", encoding=enc, newline="\n") as f:
                for r in rs:
                    f.write(tmpl.replace("{word}", r[0]).replace("{freq}", str(r[1]))
                            .replace("{pos}", r[2]).replace("{cls}", r[3]) + "\n")
    return len(sel), per_class


def build_rows(words):
    rows = []
    for w, rec in words.items():
        f = int(round(rec["f"]))
        if f <= 0:
            continue
        rows.append((w, f, rec.get("p", "—"), classify(w, rec.get("p", "—"))))
    rows.sort(key=lambda r: -r[1])
    return rows


def export_all(words, bg, cfg, out_dir, today, write=True):
    ocfg = cfg["output"]
    enc = ocfg.get("encoding", "utf-8")
    if write:
        os.makedirs(out_dir, exist_ok=True)
    rows = build_rows(words)
    produced, per_class = {}, Counter()
    for fmt in ocfg.get("formats", []):
        t = fmt.get("type")
        path = os.path.join(out_dir, fmt.get("file", "out.txt"))
        if t == "juzi":
            n = export_juzi(rows, path, fmt, enc) if write else \
                sum(1 for w, f, _p, _c in rows
                    if f >= fmt.get("min_freq", 2) and HAN.match(w)
                    and fmt.get("min_len", 2) <= len(w) <= fmt.get("max_len", 6))
            produced[fmt.get("file")] = n
        elif t == "bigram":
            n = export_bigram(bg, path, fmt, enc) if write else sum(
                1 for (a, b), c in bg.items() if c >= fmt.get("min_count", 2)
                and HAN.match(a) and HAN.match(b))
            produced[fmt.get("file")] = n
        elif t == "raw":
            n, pc = export_raw(rows, out_dir, fmt, enc, today) if write else (
                sum(1 for r in rows if r[1] >= fmt.get("min_freq", 2)), Counter())
            produced[fmt.get("file")] = n
            per_class = pc
    return rows, produced, per_class


def write_report(path, today, rows, produced, new_words, lost_words, stats, errors, n_bigram):
    L = ["# 词库构建报告 %s" % today, ""]
    L.append("- 词条总数：**%d**　bigram 词对：**%d**" % (len(rows), n_bigram))
    L.append("- 本次新增：**%d**　淘汰：**%d**" % (len(new_words), len(lost_words)))
    L.append("- 成功源：%d / %d　失败：%d" % (
        sum(1 for s in stats if s.get("ok")), len(stats), len(errors)))
    L.append("")
    L.append("## 产出")
    L.append("")
    for k, v in produced.items():
        L.append("- `%s`：%d 行" % (k, v))
    L.append("")
    L.append("## 数据源")
    L.append("")
    L.append("| 源 | 状态 | 类型 | 字数 | 句数 | 唯一词 | bigram | 备注 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for s in stats:
        if s.get("ok"):
            L.append("| %s | ✅ | %s | %s | %s | %s | %s | %s |" % (
                s["name"], s.get("type", ""), format(s["chars"], ","),
                s.get("sentences", 0), format(s["unique"], ","), s.get("bigrams", 0),
                str(s.get("note", ""))[:40]))
        else:
            L.append("| %s | ❌ | - | - | - | - | - | %s |" % (
                s["name"], str(s.get("error", ""))[:80].replace("|", "/")))
    L.append("")
    if new_words:
        L.append("## 新增词 Top 50")
        L.append("")
        L.append("　".join("%s(%d)" % (w, f) for w, f in new_words[:50]))
        L.append("")
    if lost_words:
        L.append("## 淘汰词 Top 50")
        L.append("")
        L.append("　".join(lost_words[:50]))
        L.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="水墨词库每日增量构建")
    ap.add_argument("-c", "--config", default=os.path.join(ROOT, "config", "sources.yaml"))
    ap.add_argument("--only", help="只跑名字匹配的源（子串）")
    ap.add_argument("--url", action="append", default=[], help="临时源，可重复")
    ap.add_argument("--proxy")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date")
    a = ap.parse_args()

    cfg = load_config(a.config)
    srcs = [s for s in cfg["sources"] if s.get("enabled", True)]
    if a.only:
        srcs = [s for s in srcs if a.only in str(s.get("name", ""))]
    srcs += [{"name": "临时源%d" % (i + 1), "url": u, "weight": 1.0}
             for i, u in enumerate(a.url)]
    if not srcs:
        sys.exit("没有可用的数据源，检查 config/sources.yaml")
    if all("示例" in str(s.get("name", "")) for s in srcs):
        print("⚠️  当前只启用了示例源，产物仅供测试。", file=sys.stderr)

    today = a.date or dt.date.today().isoformat()
    d = cfg["output"]["dir"]
    out_dir = d if os.path.isabs(d) else os.path.join(ROOT, d)
    state_path = os.path.join(out_dir, "state.json")
    bg_path = os.path.join(out_dir, "state_bigram.json")

    if a.reset:
        state = {"last_run": None, "words": {}}
        bg = {}
    else:
        state = load_state(state_path)
        bg = load_bigram(bg_path)
    old_words = set(state.get("words", {}))

    today_scores, pos_all, src_of = {}, {}, {}
    stats, errors = [], []
    bg_today = Counter()
    delay = cfg["fetch"].get("delay", 1.5)

    for i, src in enumerate(srcs):
        name = src.get("name") or src.get("url")
        print("[%d/%d] %s …" % (i + 1, len(srcs), name), file=sys.stderr, end="", flush=True)
        t0 = time.time()
        try:
            freq, pos_of, _sents, bg1, stat = process_source(src, cfg, a.proxy)
            weight = src.get("weight", cfg["scoring"]["default_weight"])
            for w, s in score_source(freq, weight, cfg["scoring"]["cap_per_source"]).items():
                today_scores[w] = today_scores.get(w, 0) + s
                pos_all.setdefault(w, pos_of.get(w, "—"))
                src_of.setdefault(w, []).append(name)
            bg_today.update(bg1)
            stats.append(stat)
            print(" ok  %s字 / %s词 / %s bigram (%.1fs)" % (
                format(stat["chars"], ","), format(stat["unique"], ","),
                stat["bigrams"], time.time() - t0), file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            errors.append({"name": name, "error": str(e)})
            stats.append({"name": name, "ok": False, "error": str(e)[:200]})
            print(" 失败：%s" % e, file=sys.stderr)
        if i < len(srcs) - 1:
            time.sleep(delay)

    if not today_scores:
        print("所有源都失败了，本次不做任何改动。", file=sys.stderr)

    state, dropped = merge_state(state, today_scores, pos_all, src_of, cfg, today)
    if bg_today:
        bg = merge_bigram(bg, bg_today, cfg)

    rows, produced, _pc = export_all(state["words"], bg, cfg, out_dir, today,
                                     write=not a.dry_run)

    new_words = sorted(((w, int(round(state["words"][w]["f"])))
                        for w in state["words"] if w not in old_words), key=lambda x: -x[1])

    if not a.dry_run:
        nb = produced.get("shuimo_bigram.txt", 0)
        write_report(os.path.join(out_dir, "report-%s.md" % today), today, rows,
                     produced, new_words, dropped, stats, errors, nb)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        with open(bg_path, "w", encoding="utf-8") as f:
            json.dump({" ".join(k): v for k, v in bg.items()}, f, ensure_ascii=False)

    print("完成：词条 %d（%s），bigram %d，新增 %d，淘汰 %d，失败源 %d" % (
        len(rows), ", ".join("%s=%d" % (k, v) for k, v in produced.items()),
        len(bg), len(new_words), len(dropped), len(errors)), file=sys.stderr)


if __name__ == "__main__":
    main()
