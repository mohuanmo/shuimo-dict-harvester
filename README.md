# 水墨词库自动采集器 · shuimo-dict-harvester

给「水墨」输入法用的词库流水线：**每天定时抓取若干站点 → 抽取正文 → 断句 → 分词 → 统计词频与词对共现 → 分类 → 增量累加 → 导出词库**。

## 产出（两个文件严格分开，互不掺东西）

| 文件 | 内容 | 对应格式 |
|---|---|---|
| `data/dict/shuimo_juzi.dict` | **只有** `词条<TAB>拼音<TAB>词频` | ① 组句词库（连续拼音 / 长句组句） |
| `data/dict/shuimo_bigram.txt` | **只有** `词1 词2 共现次数` | ② 语料词对（消同音歧义） |
| `data/dict/shuimo.dict` | `词<TAB>词频<TAB>词性<TAB>分类` | ③ 附带的通用词库，备查用，不喂给引擎也行 |

**① 组句词库**：词条纯汉字 2–6 字（超长跳过）；拼音无声调，`pinyin_sep: ""` 出连体 `kaishi`，`" "` 出分词 `kai shi`；词频是合并后的整数。

```
开始	kai shi	17500
讨论	taolun	32046
```

**② 语料词对**：两边各 1–4 个汉字，空格分隔，共现次数是**真实整数计数**（不乘权重、不做衰减）；打分交给引擎的 `ln(1+n)/4`，≥50 封顶 1.0。词对取自「同一句内、剔除虚词后的相邻词」，标点处断开。

```
开始 讨论 1523
非常 感谢 980
```

## 数据源（当前 14 个）

| 源 | 类型 | 说明 |
|---|---|---|
| B站-热门列表 | `bilibili_popular` | 标题/简介/分区/UP主，覆盖各分区 |
| B站-排行榜 | `bilibili_ranking` | 与热门互补 |
| **B站-热门视频评论区** | `bilibili_comments` | wbi 签名的 `/x/v2/reply/wbi/main`，热门评论 + 最新评论 + 楼中楼，权重最高（口语最真） |
| **B站-热门视频弹幕** | `bilibili_danmaku` | `/x/v2/dm/web/seg.so` 的 protobuf，按段抓取，一句话一条 |
| 人民网-时政 | `rss` | 规范书面语 |
| 中国新闻网-滚动 | `rss` | 社会新闻 |
| 澎湃新闻 | `rss` | 经 feedx 镜像，原站对 CI 403 |
| IT之家 | `rss` | 科技/数码词 |
| 少数派 | `rss` | 效率/数码 |
| 爱范儿 | `rss` | 消费科技 |
| 机核 | `rss` | 游戏/亚文化 |
| 阮一峰周刊 | `rss` | 技术+生活，术语密度高 |
| Solidot | `rss` | 科技简讯 |
| 古诗文网 | `html` | 成语/文言，给组句词库补"雅"的那一面 |

**为什么是这些**：维基/谷歌国内到不了，CI 里只会天天报错。B 站是中文口语密度最高的公开语料，评论区 + 弹幕补的正是输入法最缺的日常表达；新闻类 RSS 补规范书面语；古诗文补成语。除 B 站外全部走 RSS，因为门户首页 80% 是链接列表，启发式抽取只会拿到导航词。

曾经探测过但当前不可用的：36氪 / 虎嗅 / 品玩 / 煎蛋 / 果壳 / 中国日报 / 环球网 / 三联 / 知乎 / 科学网（返回 HTML、403、404 或超时）。配置里留了注释项，哪天好了改 `enabled: true` 即可。

## 抓歪了怎么办：六层体检

词库最怕的不是抓不到，而是把 404 页、登录墙、空白 JSON 当正文灌进去。`src/fetchx.py` 每一层都拦：

1. **HTTP 状态**：非 2xx 直接判死（401/403/404/412/5xx 全部拒绝）；
2. **Content-Type 对得上**：RSS 必须能解析出 `<item>/<entry>`，JSON 必须能解析；
3. **解压**：gzip / deflate 自动解，brotli 没装依赖就明确报错，绝不把压缩流当文本；
4. **错误页特征词**：`页面不存在`/`访问异常`/`请开启 JavaScript`/`人机验证`/`Just a moment` 等强特征命中即扔；`404`/`Not Found` 这类弱特征只在短页面时才算数；
5. **正文体检**：字数下限、汉字占比 ≥30%、唯一字占比 ≥10%（挡"加载中…"和填充页）；
6. **接口语义**：B 站 `code != 0`、`data` 为空、列表为空 → 直接判死。

任何一层不过，这个源当场跳过，**一个词都不会写进 state 和词库**，报告里记一条 `❌` 和原因。单个源失败不影响其它源；全部失败时退出码仍为 0（不给你发失败邮件），但报告会写清楚。

## 目录

```
config/sources.yaml         数据源 + 输出格式 + 打分参数
src/wordbank.py             抓取/抽取/断句/分词（单页工具，可独立使用）
src/fetchx.py               带体检的 HTTP 层（状态码/编码/错误页/正文质量）
src/sources_bili.py         B 站：热门/排行/评论(wbi)/弹幕(protobuf)
src/sources_rss.py          RSS/Atom 解析
src/harvest.py              调度 + 增量合并 + 分类 + 导出（主入口）
data/dict/                  产物 + state.json / state_bigram.json
.github/workflows/daily.yml 每日定时
```

## 快速开始

```bash
pip install -r requirements.txt

python3 src/harvest.py --reset                 # 第一次：从零建
python3 src/harvest.py                         # 之后：增量
python3 src/harvest.py --only 弹幕             # 只跑匹配的源
python3 src/harvest.py --url data/sample.html  # 临时加一个源（支持本地 html/mhtml）
python3 src/harvest.py --dry-run               # 只统计不落盘

python3 src/wordbank.py ./page.mhtml -n 80 -v  # 单页调试工具
```

## 增量与打分

- 词频存 `state.json`，每天只加当天贡献；`half_life_days: 90` 衰减，`max_age_days` / `drop_below` 淘汰，词库会新陈代谢。
- 单源计分 `min(次数 × weight, cap_per_source)`，某个站刷量也压不爆整体词频。
- bigram 走另一份 `state_bigram.json`，只累加真实整数，不衰减；`max_rows: 300000` 封顶。
- 英文统一小写归并，`CPU`/`cpu` 不会变成两条。

## 定时

`30 18 * * *`（UTC）= **北京时间每天 02:30**。cron 是 UTC，改时间记得换算并避开 `:00`；仓库 60 天无提交会自动停用 schedule，这个工作流每天都 commit，天然保活。也可手动触发，支持 `only` / `reset` 输入。

## 已知限制

- 评论区/弹幕全靠公开接口，B 站改签名或收紧风控时会整源失败（有体检兜底，不会写脏数据，但当天少一份语料）。
- `state_bigram.json` 会随词对增长（20 万对约 6–8 MB），规模大了建议把 state 挪到 Release 附件。
- Actions artifact 配额满会导致快照上传失败，已加 `continue-on-error`。
- 正文抽取是启发式的，纯 JS 渲染页面无效——这类站点请优先找 RSS / JSON 接口。

## 合规

只取公开页面与公开接口的文字，遵守 `robots.txt`，请求有延迟；不采集个人信息、不转存视频音频、不做全站镜像；词库只保留词、词频、拼音这类统计产物。商用前请自行确认各数据源条款。

## License

MIT
