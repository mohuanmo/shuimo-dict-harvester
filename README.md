# 水墨词库自动采集器 · shuimo-dict-harvester

给「水墨」输入法用的词库流水线：**每天定时抓取若干网页/接口 → 抽取正文 → 断句 → 分词 → 统计词频与词对共现 → 分类 → 增量累加 → 导出词库**。

产出三份文件，直接对应水墨的两种词库格式：

| 文件 | 格式 | 用途 |
|---|---|---|
| `data/dict/shuimo_juzi.dict` | `词条<TAB>拼音<TAB>词频` | **① 组句词库**，连续拼音 / 长句组句 |
| `data/dict/shuimo_bigram.txt` | `词1 词2 共现次数` | **② 语料词对**，消同音歧义 |
| `data/dict/shuimo.dict` | `词<TAB>词频<TAB>词性<TAB>分类` | ③ 通用词库，备查/其他输入法用 |

```
┌────────────┐   ┌──────────┐   ┌────────┐   ┌────────────┐   ┌──────────┐
│ config/    │──▶│ 抓取      │──▶│ 正文    │──▶│ 断句 + 分词 │──▶│ 词频/词对 │
│ sources.   │   │ html/rss │   │ 抽取    │   │ jieba+新词  │   │ 单源封顶  │
│ yaml       │   │ /B站 API  │   │ HTMLP.  │   │ 合并        │   │          │
└────────────┘   └──────────┘   └────────┘   └────────────┘   └────┬─────┘
                                                                   ▼
┌────────────┐   ┌──────────┐   ┌────────┐   ┌────────────┐   ┌──────────┐
│ Actions    │◀──│ commit   │◀──│ 导出    │◀──│ 分类       │◀──│ 增量合并  │
│ 每日 cron   │   │ 回仓库    │   │ 三种格式 │   │ 规则+词性   │   │ 衰减+淘汰 │
└────────────┘   └──────────┘   └────────┘   └────────────┘   └──────────┘
```

## 输出格式（严格按水墨的规范）

**① 组句词库**
```
开始	kai shi	17500
讨论	taolun	32046
```
- 词条：纯汉字，2–6 字（`max_len` 可调），超长跳过；
- 拼音：无声调，`pinyin_sep: ""` 出连体（`kaishi`），`" "` 出分词（`kai shi`）；
- 词频：合并后的整数，越大越靠前。

**② 语料词对**
```
开始 讨论 1523
非常 感谢 980
```
- 两边各 1–4 个汉字，空格分隔，共现次数是**真实整数计数**（不做衰减、不乘权重）；
- 打分交给引擎：`ln(1+次数)/4`，≥50 封顶 1.0；
- 词对来自「同一句内、剔除虚词后的相邻词」，标点处断开。

**③ 通用词库**：`词\t词频\t词性\t分类`，另按分类拆到 `data/dict/classes/`。

## 数据源

默认开这六个（改 `config/sources.yaml` 就能加减）：

| 源 | 类型 | 为什么是它 |
|---|---|---|
| B站-热门列表 | `bilibili_popular` | 标题/简介/分区/UP主，短句多、口语化，正好补输入法缺的日常表达 |
| B站-排行榜 | `bilibili_ranking` | 与热门互补，覆盖不同分区的高热内容 |
| B站-热门弹幕 | `bilibili_danmaku` | 弹幕是最"人话"的语料，组句词库和 bigram 都靠它提鲜 |
| 少数派 | `rss` | 科技/效率圈用词规范，RSS 正文干净 |
| 中国新闻网-滚动 | `rss` | 时政社会类的规范书面语，补正式词 |
| 阮一峰周刊 | `rss` | 技术+生活混合，术语密度高 |

**为什么用 RSS 而不是抓首页**：门户首页 80% 是链接列表，启发式抽取拿到的全是导航词；RSS 的 `description` 就是正文，结构稳定、没有 JS。
**为什么不用维基/谷歌**：国内网络到不了，CI 里失败只是徒增噪音。GitHub 的仓库页能通，但正文以英文为主，对中文词库贡献低，所以没放。

B 站走的是公开 JSON 接口（`/x/web-interface/popular`、`/ranking/v2`、`comment.bilibili.com/{cid}.xml`），不带登录态、不碰视频流，只取文字。弹幕接口偶尔对海外 IP 返回 412，抓不到就整源跳过，不影响其它源。

## 目录

```
config/sources.yaml         数据源 + 输出格式 + 打分参数
src/wordbank.py             抓取/抽取/断句/分词（单页工具，可独立使用）
src/sources_bili.py         B 站接口（热门/排行/弹幕/详情）
src/sources_rss.py          RSS/Atom 解析
src/harvest.py              调度 + 增量合并 + 分类 + 导出（主入口）
data/dict/                  产物 + state.json（历史词频）
data/dict/classes/          按分类拆分的通用词库
.github/workflows/daily.yml 每日定时
```

## 快速开始

```bash
pip install -r requirements.txt

python3 src/harvest.py --reset                 # 第一次：从零建
python3 src/harvest.py                         # 之后：增量
python3 src/harvest.py --only 弹幕             # 只跑匹配的源
python3 src/harvest.py --url data/sample.html  # 临时加一个源
python3 src/harvest.py --dry-run               # 只统计不落盘

# 单页调试工具
python3 src/wordbank.py ./page.mhtml -n 80 -v
```

## 增量与打分

- 词频存在 `state.json`，每天只加当天贡献；`half_life_days: 90` 让旧词自然衰减，`max_age_days` / `drop_below` 负责淘汰，词库会新陈代谢。
- 单源计分 `min(次数 × weight, cap_per_source)`：某个站刷量也压不爆整体词频。
- 同一个源挂了只记一条 error，其余照跑；全挂时退出码仍为 0（不给你发失败邮件），但报告里写清楚。
- 英文统一小写归并，`CPU` / `cpu` 不会变成两条。

## 定时

`30 18 * * *`（UTC）= **北京时间每天 02:30**。注意两点：

- cron 是 UTC，改时间记得换算，并且避开 `:00` 整点排队；
- 仓库 60 天无提交会**自动停用 schedule**。这个工作流每天都 commit 一次，天然保活。

也可以在 Actions 页面手动触发，支持 `only` / `reset` 两个输入。

## 已知限制

- `state_bigram.json` 会随词对增长（20 万对约 6–8 MB），`max_rows` 已封顶 30 万；仓库长期跑会变胖，规模大了建议把 state 挪到 Release 附件。
- 账号的 Actions artifact 配额满了会导致快照上传失败，已加 `continue-on-error`，不影响词库提交。
- 正文抽取是启发式的，对纯 JS 渲染的页面无效——这种站点优先找它的 RSS 或 JSON 接口。
- B 站接口没有登录态，某些分区/稿件会返回 412 或空列表；需要更稳就加 cookie，但会增加失效风险。

## 合规

- 只取公开页面与公开接口的文字，遵守 `robots.txt`，请求有延迟；
- 不采集个人信息，不转存视频/音频，不做全站镜像；
- 词库只保留词、词频、拼音这类统计产物，不含原文段落；
- 商用前请自行确认各数据源的条款与版权。

## License

MIT
