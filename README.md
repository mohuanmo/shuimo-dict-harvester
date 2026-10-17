# 水墨词库自动采集器 · shuimo-dict-harvester

给「水墨」输入法用的词库流水线：**每天定时抓取若干网页 → 抽取正文 → 断句 → 中文分词 → 过滤 → 分类 → 增量累加 → 导出词库**。

产出是一个纯文本词库（`data/dict/shuimo.dict`），外加按分类拆分的子词库、当日变更报告和机器可读的 `meta.json`。

```
┌────────────┐   ┌──────────┐   ┌────────┐   ┌────────────┐   ┌──────────┐
│ config/    │──▶│ 抓取      │──▶│ 正文    │──▶│ 断句 + 分词 │──▶│ 过滤打分  │
│ sources.   │   │ urllib   │   │ 抽取    │   │ jieba+新词  │   │ 单源封顶  │
│ yaml       │   │ +robots  │   │ HTMLP.  │   │ 合并        │   │          │
└────────────┘   └──────────┘   └────────┘   └────────────┘   └────┬─────┘
                                                                   ▼
┌────────────┐   ┌──────────┐   ┌────────┐   ┌────────────┐   ┌──────────┐
│ Actions    │◀──│ commit   │◀──│ 导出    │◀──│ 分类       │◀──│ 增量合并  │
│ 每日 cron   │   │ 回仓库    │   │ dict   │   │ 规则+词性   │   │ 衰减+淘汰 │
└────────────┘   └──────────┘   └────────┘   └────────────┘   └──────────┘
```

## 特点

- **零重依赖**：只用标准库 + `jieba` + `PyYAML`，GitHub Actions 免费额度绰绰有余（一次运行十几秒到几分钟）。
- **增量而非全量**：词频存在 `data/dict/state.json`，每天只累加当天的分数，历史带半衰期衰减，所以词库会自己"新陈代谢"。
- **单源封顶**：一个词在同一个源里最高只计 `cap_per_source` 分，避免某个站点刷量把词频刷爆。
- **抗震**：某个源抓挂了只记一条错误，其余源照常跑；全挂时退出码仍为 0，不会每天给你发失败邮件（但报告里写清楚）。
- **可追溯**：`report-YYYY-MM-DD.md` 里有每个源的字数/句数/唯一词数、新增词 Top50、淘汰词、分类分布。

## 目录

```
config/sources.yaml        数据源与口味配置（改这里，不动代码）
src/wordbank.py            抓取 / 正文抽取 / 断句 / 分词（单页版，可单独用）
src/harvest.py             多源调度 + 增量合并 + 分类 + 导出（主入口）
data/dict/shuimo.dict      产出的词库
data/dict/classes/*.txt    按分类拆分的词库
data/dict/state.json       历史词频状态（增量用）
data/dict/meta.json        本次运行的机器可读统计
data/dict/report-*.md      人类可读报告
data/raw/                  原始正文（.gitignore 掉，只在 CI 临时用）
.github/workflows/daily.yml 每日定时工作流
```

## 快速开始

```bash
git clone https://github.com/<you>/shuimo-dict-harvester.git
cd shuimo-dict-harvester
pip install -r requirements.txt

# 先单页试一下水（不落盘，只打印统计）
python3 src/wordbank.py ./某个页面.html -n 80 --min-freq 2 -v

# 跑完整流水线
python3 src/harvest.py --reset          # 第一次：从零建
python3 src/harvest.py                  # 之后：增量
python3 src/harvest.py --only 人民网     # 只跑指定的源
python3 src/harvest.py --url ./a.html --dry-run
```

`wordbank.py` 是单页版工具，输出「词语 / 词性 / 频次 / 占比 / 例句 + 句子库」，支持本地 html、`.mhtml`（浏览器另存）、stdin、以及 `--proxy`：

```bash
python3 src/wordbank.py https://example.com/a
python3 src/wordbank.py ./page.mhtml -n 80 --why 薇帕
```

## 配置

`config/sources.yaml` 三个块：

| 块 | 关键项 | 说明 |
|---|---|---|
| `output` | `template` | 词库行模板，默认 `{word}\t{freq}\t{pos}\t{cls}`。水墨若只吃「词 词频」就改成 `"{word} {freq}"` |
| | `min_freq` | 导出阈值，默认 2；增量模式下新词会逐日涨频，第一次跑可能偏少 |
| | `split_by_class` | 是否额外按分类拆文件 |
| `scoring` | `cap_per_source` | 单词单源封顶分 |
| | `half_life_days` | 历史衰减半衰期，0 = 不衰减（词库只增不减） |
| | `max_age_days` / `drop_below` | 淘汰条件 |
| `fetch` | `delay` / `retries` / `respect_robots` | 礼貌抓取，默认遵守 robots.txt |
| `sources` | `name/url/weight/enabled` | 想加源就往数组里加，`enabled: true` 才生效 |

## 分类

两级规则，都在 `src/harvest.py` 里，改 `CLASS_RULES` 就行：

1. **词性优先**：`nr/ns/nt/nz/i/l/j` → 人名 / 地名 / 机构 / 专名 / 成语 / 惯用语 / 简称；
2. **关键词命中**：科技数码、财经商业、医疗健康、体育竞技、文教文史、时政社会、生活日常、饮食烹饪、自然地理、网络流行；
3. 兜底按词性大类（名词 / 动词 / 形容词 …）。

## 定时

`.github/workflows/daily.yml` 默认 `30 18 * * *`（UTC）= **北京时间每天 02:30**。两个坑要注意：

- **cron 是 UTC**，改时间记得换算；整点附近会排队，建议错开 `:00`。
- **仓库 60 天无提交，schedule 会被自动停用**。这个工作流每天都会 commit 一次 `data/dict`，所以天然保活；但如果哪天所有源都挂了导致没有 diff，就可能积累——所以别让数据源长期全灭。

也可以在 Actions 页面手动触发（`workflow_dispatch`），可传 `only` / `reset`。

## 接到水墨上

1. 确认水墨的词库格式和大小上限（纯文本 `词 词频[ 词性]` / 二进制 bin / sqlite，差别很大）；
2. 按需改 `output.template`，或加一个 `src/export_<format>.py` 做转换；
3. 让水墨订阅 `https://raw.githubusercontent.com/<you>/shuimo-dict-harvester/main/data/dict/shuimo.dict`，或手动导入。

## 已知限制

- **示例源默认开着**（`config/sources.yaml` 里的「示例-本地页面」），只为让新仓库第一次跑就是绿的。上线前务必 `enabled: false`，否则词库会混进测试文本。
- `state.json` 随词条线性增长（10 万词约 8–15 MB），每天全量提交一次，仓库体积会慢慢变大。词库规模大了以后建议把 state 挪到 Actions Cache 或 Release 附件，仓库里只留成品 dict。
- 账号的 **Actions artifact 存储配额**如果满了，上传会失败；工作流里已加 `continue-on-error`，不影响词库提交。
- 正文抽取是启发式的（按块级标签 + 丢弃 nav/footer），对强 JS 渲染的站点无效，那种站点要么换静态镜像，要么上 Playwright（会显著增加 CI 时间）。

## 合规

- 只抓取公开页面，默认遵守 `robots.txt`，请求带延迟；
- 不采集个人信息，不整站转存，语料只用于统计词频；
- 词库是统计产物（词 + 频次），不含原文段落；
- 商用前请自行确认各数据源的条款与版权。

## License

MIT（`src/wordbank.py` 为作者原有脚本）
