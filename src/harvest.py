#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
harvest.py —— 水墨词库每日增量构建

流水线：
    多源抓取 → 正文抽取 → 断句 → 分词（jieba + 未知词合并）
    → 过滤 → 单源封顶计分 → 历史状态衰减合并 → 分类 → 导出词库 + 变更报告

设计要点：
  1. 增量：词频存在 data/dict/state.json，每天只往上累加（带半衰期衰减），
     不做全量重算，几十万词也不会慢。
  2. 抗震：单个源抓挂了只记一条 error，不影响其它源；全部挂掉时退出码仍为 0
     （避免 GitHub Actions 每天报红），但会在报告里写清楚。
  3. 可控：单个源对单个词的贡献封顶，防止某个刷量站点把词频刷爆。

用法：
    python3 src/harvest.py                       # 按 config/sources.yaml 跑
    python3 src/harvest.py --only 示例-本地页面   # 只跑一个源
    python3 src/harvest.py --url page.html       # 临时加一个源（调试用）
    python3 src/harvest.py --reset               # 丢掉历史状态，从零重建
    python3 src/harvest.py --dry-run             # 只打印统计，不写文件
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

import wordbank as wb  # noqa: E402  复用已有的抓取/抽取/分词实现

try:
    import yaml
except ImportError:  # 没装 pyyaml 时退化成 json 配置
    yaml = None


# ---------------------------------------------------------------- 配置

DEFAULTS = {
    "output": {
        "dir": "data/dict",
        "template": "{word}\t{freq}\t{pos}\t{cls}",
        "min_freq": 2,
        "max_words": 0,
        "split_by_class": True,
        "encoding": "utf-8",
    },
    "scoring": {
        "cap_per_source": 300,
        "default_weight": 1.0,
        "half_life_days": 90,
        "max_age_days": 400,
        "drop_below": 0.8,
    },
    "fetch": {
        "delay": 1.5,
        "timeout": 25,
        "retries": 2,
        "respect_robots": True,
        "save_raw": True,
    },
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
    """遵守 robots.txt；读不到就当作允许（大多数站点没有或不可达）"""
    from urllib.parse import urlparse
    from urllib import robotparser
    p = urlparse(url)
    if not p.scheme.startswith("http"):
        return True
    rp = robotparser.RobotFileParser()
    rp.set_url(f"{p.scheme}://{p.netloc}/robots.txt")
    try:
        rp.read()
        return rp.can_fetch(ua, url)
    except Exception:
        return True


def fetch_with_retry(url, fcfg, proxy=None):
    last = None
    for i in range(fcfg["retries"] + 1):
        try:
            return wb.fetch(url, proxy)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < fcfg["retries"]:
                time.sleep(2 + i * 2)
    raise last


# ---------------------------------------------------------------- 单源处理

def process_source(src, cfg, proxy=None):
    """抓一个源 → 返回 (词频Counter, 词性dict, 句子list, 统计dict)"""
    fcfg = cfg["fetch"]
    url = src["url"]
    name = src.get("name") or url

    if fcfg.get("respect_robots") and not robots_allowed(url, wb.UA):
        raise PermissionError("robots.txt 不允许抓取")

    html, real_url, note = fetch_with_retry(url, fcfg, proxy)

    ex = wb.Extractor()
    ex.feed(html)
    text = ex.text()
    title = re.sub(r"\s+", " ", ex.title).strip()
    if title and text.startswith(title):
        text = text[len(title):].lstrip(" \n-_·—|")
    if len(text) < 50:
        raise ValueError("正文太短（可能是 JS 渲染），请换静态页或用浏览器另存 .mhtml")

    sents, sst = wb.split_sentences(text)
    tokens, engine, tinfo = wb.tokenize(text)

    freq, pos_of, dropped = Counter(), {}, 0
    for w, flag in tokens:
        if wb.check_word(w):
            dropped += 1
            continue
        # 英文一律小写归并，避免 CPU / cpu 分成两个词条
        if re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", w):
            w = w.lower()
        freq[w] += 1
        if w not in pos_of:
            pos_of[w] = wb.pos_cn(flag)

    if fcfg.get("save_raw"):
        try:
            raw_dir = os.path.join(ROOT, "data", "raw")
            os.makedirs(raw_dir, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name)[:40]
            with open(os.path.join(raw_dir, f"{stamp}-{safe}.txt"), "w",
                      encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    stat = {
        "name": name, "url": real_url, "ok": True,
        "chars": len(text), "sentences": len(sents),
        "tokens": len(tokens), "kept": sum(freq.values()),
        "dropped": dropped, "unique": len(freq),
        "engine": engine, "note": note,
        "top": freq.most_common(10),
    }
    return freq, pos_of, sents, stat


# ---------------------------------------------------------------- 计分

def score_source(freq, weight, cap):
    """单源计分：count * weight，但单词得分封顶，防止刷量"""
    return {w: min(c * weight, cap) for w, c in freq.items()}


# ---------------------------------------------------------------- 分类

# 关键词命中即归入该分类；先命中先算，命中数最多的类胜出
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
                "出门", "早上", "晚上", "手机", "咖啡", "超市", "快递", "装修", "房租"],
    "饮食烹饪": ["食材", "做法", "锅", "火", "油", "盐", "糖", "酱", "蒸", "炒", "煮",
                "烤", "味道", "口感", "recipe", "餐厅", "菜谱", "早茶"],
    "自然地理": ["山", "河", "湖", "海", "森林", "气候", "温度", "降雨", "台风", "地震",
                "生态", "物种", "植物", "动物", "湿 地", "高原", "沙漠", "海洋"],
    "网络流行": ["网友", "热搜", "视频", "直播", "评论区", "粉丝", "点赞", "转发", "破防",
                "emo", "整活", "绝了", "yyds", "弹幕", "up主", "打卡"],
}

POS_CLASS = {
    "人名": "人名", "地名": "地名", "机构": "机构", "专名": "专名",
    "成语": "成语", "惯用语": "惯用语", "简称": "简称",
    "英文": "英文", "拟声词": "拟声词", "叹词": "语气",
}


def classify(word, pos):
    """先看词性（人名/地名/专名），再看关键词，最后退回词性大类"""
    if pos in POS_CLASS:
        return POS_CLASS[pos]
    hits = Counter()
    for cls, kws in CLASS_RULES.items():
        for k in kws:
            if k in word:
                hits[cls] += 1
    if hits:
        return hits.most_common(1)[0][0]
    return {
        "名词": "通用名词", "动词": "动词", "形容词": "形容词", "副词": "副词",
        "数词": "数词", "量词": "量词", "代词": "代词", "时间词": "时间",
        "未标注": "未标注", "—": "未标注",
    }.get(pos, "其他")


# ---------------------------------------------------------------- 状态合并

def load_state(path):
    if not os.path.exists(path):
        return {"last_run": None, "words": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def merge_state(state, today_scores, pos_of, src_of, cfg, today):
    sc = cfg["scoring"]
    words = state.get("words", {})
    last = state.get("last_run")
    delta_days = 1
    if last:
        try:
            delta_days = max(1, (dt.date.fromisoformat(today) -
                                 dt.date.fromisoformat(last)).days)
        except ValueError:
            delta_days = 1

    hl = sc.get("half_life_days") or 0
    if hl > 0:
        factor = 0.5 ** (delta_days / hl)
        for w, rec in words.items():
            rec["f"] *= factor

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

    # 淘汰：太旧 或 衰减到很小
    max_age = sc.get("max_age_days") or 0
    floor = sc.get("drop_below") or 0
    drop = []
    for w, rec in words.items():
        if max_age:
            try:
                age = (dt.date.fromisoformat(today) -
                       dt.date.fromisoformat(rec["last"])).days
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


# ---------------------------------------------------------------- 导出

def export(words, cfg, out_dir, today, stats, errors, write=True):
    ocfg = cfg["output"]
    os.makedirs(out_dir, exist_ok=True)
    tmpl = ocfg["template"]
    min_freq = ocfg["min_freq"]
    enc = ocfg.get("encoding", "utf-8")

    rows = []
    for w, rec in words.items():
        f = int(round(rec["f"]))
        if f < min_freq:
            continue
        cls = classify(w, rec.get("p", "—"))
        rows.append((w, f, rec.get("p", "—"), cls))
    rows.sort(key=lambda r: -r[1])
    if ocfg.get("max_words"):
        rows = rows[:ocfg["max_words"]]

    def render(r):
        w, f, p, c = r
        return (tmpl.replace("{word}", w).replace("{freq}", str(f))
                   .replace("{pos}", p).replace("{cls}", c))

    per_class = Counter()
    for r in rows:
        per_class[r[3]] += 1

    if write:
        os.makedirs(out_dir, exist_ok=True)
        main_path = os.path.join(out_dir, "shuimo.dict")
        with open(main_path, "w", encoding=enc, newline="\n") as f:
            f.write(f"# 水墨词库  生成于 {today}  共 {len(rows)} 词\n")
            for r in rows:
                f.write(render(r) + "\n")

    if write and ocfg.get("split_by_class"):
        cdir = os.path.join(out_dir, "classes")
        os.makedirs(cdir, exist_ok=True)
        bucket = {}
        for r in rows:
            bucket.setdefault(r[3], []).append(r)
        for cls, rs in bucket.items():
            safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", cls)
            with open(os.path.join(cdir, f"{safe}.txt"), "w",
                      encoding=enc, newline="\n") as f:
                for r in rs:
                    f.write(render(r) + "\n")

    meta = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "date": today,
        "words": len(rows),
        "total_score": int(sum(r[1] for r in rows)),
        "by_class": dict(per_class.most_common()),
        "sources": stats,
        "errors": errors,
    }
    if write:
        with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    return rows, meta


def write_report(path, today, rows, meta, new_words, lost_words, stats, errors):
    lines = [f"# 词库构建报告 {today}", ""]
    lines.append(f"- 词条总数：**{len(rows)}**")
    lines.append(f"- 本次新增：**{len(new_words)}**　淘汰：**{len(lost_words)}**")
    lines.append(f"- 成功源：{sum(1 for s in stats if s.get('ok'))} / "
                 f"{len(stats)}　失败：{len(errors)}")
    lines.append("")
    lines.append("## 数据源")
    lines.append("")
    lines.append("| 源 | 状态 | 正文字数 | 句子 | 唯一词 | 耗时说明 |")
    lines.append("|---|---|---|---|---|---|")
    for s in stats:
        if s.get("ok"):
            lines.append(f"| {s['name']} | ✅ | {s['chars']:,} | {s['sentences']:,} "
                         f"| {s['unique']:,} | {s.get('engine','')} |")
        else:
            lines.append(f"| {s['name']} | ❌ | - | - | - | {s.get('error','')} |")
    lines.append("")
    if new_words:
        lines.append("## 新增词 Top 50")
        lines.append("")
        lines.append("　".join(f"{w}({f})" for w, f in new_words[:50]))
        lines.append("")
    if lost_words:
        lines.append("## 淘汰词 Top 50")
        lines.append("")
        lines.append("　".join(lost_words[:50]))
        lines.append("")
    lines.append("## 分类分布")
    lines.append("")
    for c, n in meta["by_class"].items():
        lines.append(f"- {c}：{n}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="水墨词库每日增量构建")
    ap.add_argument("-c", "--config", default=os.path.join(ROOT, "config", "sources.yaml"))
    ap.add_argument("--only", help="只跑名字匹配的源（子串匹配）")
    ap.add_argument("--url", action="append", default=[], help="临时源，可重复")
    ap.add_argument("--proxy")
    ap.add_argument("--reset", action="store_true", help="清空历史状态重建")
    ap.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    ap.add_argument("--date", help="伪造日期，便于测试衰减（YYYY-MM-DD）")
    a = ap.parse_args()

    cfg = load_config(a.config)
    srcs = [s for s in cfg["sources"] if s.get("enabled", True)]
    if a.only:
        srcs = [s for s in srcs if a.only in s.get("name", "")]
    srcs += [{"name": f"临时源{i+1}", "url": u, "weight": 1.0}
             for i, u in enumerate(a.url)]
    if not srcs:
        sys.exit("没有可用的数据源，检查 config/sources.yaml")
    if all("示例" in (s.get("name") or "") for s in srcs):
        print("⚠️  当前只启用了示例源，产物仅供测试，别直接喂给输入法。",
              file=sys.stderr)

    today = a.date or dt.date.today().isoformat()
    out_dir = os.path.join(ROOT, cfg["output"]["dir"]) if not os.path.isabs(
        cfg["output"]["dir"]) else cfg["output"]["dir"]
    state_path = os.path.join(out_dir, "state.json")

    state = {"last_run": None, "words": {}} if a.reset else load_state(state_path)
    old_words = set(state.get("words", {}))

    today_scores, pos_all, src_of, stats, errors = {}, {}, {}, [], []
    delay = cfg["fetch"].get("delay", 1.5)

    for i, src in enumerate(srcs):
        name = src.get("name") or src["url"]
        print(f"[{i+1}/{len(srcs)}] {name} …", file=sys.stderr, end="", flush=True)
        t0 = time.time()
        try:
            freq, pos_of, sents, stat = process_source(src, cfg, a.proxy)
            weight = src.get("weight", cfg["scoring"]["default_weight"])
            sc = score_source(freq, weight, cfg["scoring"]["cap_per_source"])
            for w, s in sc.items():
                today_scores[w] = today_scores.get(w, 0) + s
                pos_all.setdefault(w, pos_of.get(w, "—"))
                src_of.setdefault(w, []).append(name)
            stats.append(stat)
            print(f" ok  {stat['chars']:,}字 / {stat['unique']:,}词 "
                  f"({time.time()-t0:.1f}s)", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            errors.append({"name": name, "error": str(e)})
            stats.append({"name": name, "ok": False, "error": str(e)[:200]})
            print(f" 失败：{e}", file=sys.stderr)
        if i < len(srcs) - 1:
            time.sleep(delay)

    if not today_scores:
        print("所有源都失败了，本次不做任何改动。", file=sys.stderr)

    state, dropped = merge_state(state, today_scores, pos_all, src_of, cfg, today)
    rows, meta = export(state["words"], cfg, out_dir, today, stats, errors,
                        write=not a.dry_run)

    new_words = sorted(((w, int(round(state["words"][w]["f"])))
                        for w in state["words"] if w not in old_words),
                       key=lambda x: -x[1])
    if not a.dry_run:
        write_report(os.path.join(out_dir, f"report-{today}.md"),
                     today, rows, meta, new_words, dropped, stats, errors)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        # state.json 有几十万词会很大，单独存一份紧凑版
    print(f"完成：词条 {len(rows)}，新增 {len(new_words)}，淘汰 {len(dropped)}，"
          f"失败源 {len(errors)}", file=sys.stderr)


if __name__ == "__main__":
    main()
