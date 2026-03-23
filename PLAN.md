# 后端优化方案 — Chat Analysis Backend V2

## 一、现状问题诊断

经过完整代码分析，当前后端存在以下核心问题：

### 1. 没有中文翻译层（最关键）
- `hourly_keywords` 表存储的是**泰语原词**（如 `อยาก`, `ปัญหา`）
- 前端展示给中文母语用户时，看到的是一堆泰语关键词，**完全无法理解**
- 现有 `explain_keywords_in_chinese` 只在 `/api/insights/explain` 这一个端点做了翻译，而且是实时调 LLM，慢且贵
- `demand_signals` 虽然有中文映射，但只有 11 个硬编码模式，覆盖面极窄

### 2. 数据结构不够"前端友好"
- API 返回的是原始扁平数据（关键词列表、需求信号列表），前端很难做出直观的可视化
- 缺少：话题聚类、时间趋势、消息量曲线、热度排名变化等维度
- 没有"每日摘要"概念 — 只有离散的小时窗口，无法回答"今天泰语区聊了什么"

### 3. LLM 调用效率低
- 每次聚合都从原始消息重新走 `候选筛选 → 分块 → Stage1 → Stage2`，无增量复用
- 同步阻塞调用，分析运行时 API 无响应
- 没有结果缓存 — 同一窗口重复请求会重复花钱

### 4. 前置处理不够充分
- 4 条/秒 ≈ 34.5 万条/天，全部喂 TF-IDF 性能还可以，但全部喂 LLM 不现实
- 缺少消息去重（用户刷屏、复制粘贴）
- 缺少对话线程聚合（同一话题的消息分散在不同时间窗口）

---

## 二、优化方案总览

```
                        ┌─────────────────────────────┐
                        │      Discord Gateway        │
                        └──────────┬──────────────────┘
                                   │ on_message
                        ┌──────────▼──────────────────┐
                        │   Collector (现有，微调)      │
                        │  + 消息去重指纹              │
                        │  + channel_name 采集         │
                        └──────────┬──────────────────┘
                                   │ upsert messages
                        ┌──────────▼──────────────────┐
                        │   Hourly Aggregator (增强)    │
                        │  1. TF-IDF (现有)            │
                        │  2. 话题聚类 (新增)           │
                        │  3. 需求信号提取 (增强)       │
                        │  4. 消息量统计 (新增)         │
                        └──────────┬──────────────────┘
                                   │
                        ┌──────────▼──────────────────┐
                        │   翻译 & 摘要层 (新增)        │
                        │  - 关键词批量翻译+缓存        │
                        │  - 话题聚类中文命名           │
                        │  - 小时摘要 → 中文            │
                        │  - 每日 Digest 合成           │
                        └──────────┬──────────────────┘
                                   │
                        ┌──────────▼──────────────────┐
                        │   API Layer (重构)            │
                        │  面向前端的结构化 JSON        │
                        └─────────────────────────────┘
```

---

## 三、具体改动点

### 改动 1：新增 `keyword_translations` 缓存表

**目的**：泰语关键词只翻译一次，后续查询直接走缓存。

```python
# src/storage/models.py 新增
class KeywordTranslation(Base):
    __tablename__ = "keyword_translations"

    keyword_thai: Mapped[str] = mapped_column(String(255), primary_key=True)
    keyword_cn: Mapped[str] = mapped_column(String(255))       # 中文翻译
    keyword_en: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # 英文（备用）
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)     # 分类标签
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
```

**工作流**：
1. 每次小时聚合产出 top-50 关键词后，查 `keyword_translations` 表
2. 未命中的关键词（通常只有几个新词）批量调 LLM 翻译
3. 翻译结果写入缓存表，下次直接查库
4. LLM Prompt 示例：`"将以下泰语词翻译成中文，返回JSON数组: [{thai, cn, category}]"`

**成本估算**：每小时新增关键词通常 < 10 个，单次翻译 ~200 tokens，几乎可忽略。

---

### 改动 2：新增话题聚类模块 `src/aggregator/topic_cluster.py`

**目的**：将扁平关键词列表聚合成 3-5 个"话题组"，前端可以直观展示。

**算法**：
1. 取当前窗口 top-50 关键词的 TF-IDF 向量
2. 构建关键词共现矩阵（同一条消息中共同出现的关键词视为相关）
3. 用层次聚类（`scipy.cluster.hierarchy`）或简单的社区发现算法分成 3-5 组
4. 每组选 TF-IDF 最高的词作为代表词
5. 调 LLM 为每组生成一个 **中文话题标题**（5-10 字）

**输出结构**：
```json
[
  {
    "topic_id": 1,
    "title_cn": "游戏充值问题",
    "keywords": [
      {"keyword_thai": "เติมเงิน", "keyword_cn": "充值", "tfidf": 0.42},
      {"keyword_thai": "ไม่ได้", "keyword_cn": "不行/无法", "tfidf": 0.38}
    ],
    "message_count": 87,
    "heat_score": 0.85
  },
  ...
]
```

---

### 改动 3：新增 `daily_digests` 表和每日摘要生成

**目的**：提供"今天泰语区聊了什么"的一站式回答。

```python
# src/storage/models.py 新增
class DailyDigest(Base):
    __tablename__ = "daily_digests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    digest_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Bangkok")

    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    thai_messages: Mapped[int] = mapped_column(Integer, default=0)
    active_users: Mapped[int] = mapped_column(Integer, default=0)

    # 核心内容（全部中文）
    summary_cn: Mapped[str] = mapped_column(Text, default="")           # 200字中文日报
    top_topics: Mapped[list] = mapped_column(JSON, default=list)        # 话题聚类结果
    demand_signals: Mapped[list] = mapped_column(JSON, default=list)    # 需求信号（中文）
    keyword_cloud: Mapped[list] = mapped_column(JSON, default=list)     # 词云数据 [{cn, weight}]
    hourly_volumes: Mapped[list] = mapped_column(JSON, default=list)    # 24小时消息量曲线

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
```

**生成流程**（在 scheduler 中，每日 UTC+7 00:05 触发）：
1. 汇总当天所有 `analysis_runs`
2. 合并关键词 → 话题聚类
3. 合并需求信号 → 去重计数
4. 统计每小时消息量 → `hourly_volumes`
5. 调 LLM：输入所有小时摘要 + 话题 + 信号 → 输出 200 字中文日报

---

### 改动 4：增强需求信号提取 `DemandSignalExtractor`

**现状**：只有 11 个硬编码泰语模式词。

**改进**：
1. **扩展规则库**到 30-40 个模式，增加：
   - 比较类：`ดีกว่า`（更好）、`เทียบ`（比较）、`แนะนำ`（推荐）
   - 情感类：`ผิดหวัง`（失望）、`พอใจ`（满意）、`เบื่อ`（厌烦）
   - 行动类：`สมัคร`（注册）、`ถอน`（提现）、`ฝาก`（充值/存款）
2. **支持从配置文件动态加载**模式词（不需要改代码就能加词）
3. **信号分类细化**，增加子类型：
   - `功能诉求` → `新功能需求` / `已有功能改进`
   - `问题反馈` → `Bug报告` / `使用体验差` / `信息缺失`

---

### 改动 5：新增消息去重与质量过滤

**在 Collector 层增加**：
1. **内容指纹**：对 `cleaned_text` 做 simhash/minhash，相似度 > 0.9 的消息标记为 `is_duplicate`
2. **质量评分**：基于消息长度、是否包含有效泰语词、是否纯表情/纯链接，赋予 0-1 的 `quality_score`
3. 在 `messages` 表增加 `content_hash`, `is_duplicate`, `quality_score` 字段

**在 Aggregator 层**：
- TF-IDF 和 LLM 分析时，过滤 `is_duplicate=False AND quality_score >= 0.3` 的消息
- 预计可减少 20-40% 的无效消息，直接降低 LLM 成本

---

### 改动 6：重构 API 层 — 面向前端的结构化输出

#### 6.1 新增 `GET /api/v2/dashboard`

**一次请求获取前端所需的全部数据**，避免前端发 6 个并行请求：

```json
{
  "metrics": {
    "total_24h": 14520,
    "thai_24h": 12300,
    "thai_ratio": "84.7%",
    "active_users_24h": 342,
    "active_provider": "deepseek"
  },
  "hourly_volumes": [
    {"hour": "2026-03-23T00:00Z", "count": 580},
    {"hour": "2026-03-23T01:00Z", "count": 420},
    ...
  ],
  "top_topics": [
    {
      "title_cn": "游戏充值异常",
      "keywords_cn": ["充值", "失败", "等待"],
      "message_count": 187,
      "heat_score": 0.92,
      "trend": "rising"
    },
    ...
  ],
  "demand_signals": [
    {
      "signal_cn": "功能诉求",
      "count": 45,
      "sub_signals": [
        {"label": "新增提现方式", "count": 12, "example_cn": "希望能用XX支付提现"},
        ...
      ]
    },
    ...
  ],
  "keyword_cloud": [
    {"word_cn": "充值", "word_thai": "เติมเงิน", "weight": 0.85},
    {"word_cn": "客服", "word_thai": "บริการ", "weight": 0.72},
    ...
  ],
  "services": [...]
}
```

#### 6.2 新增 `GET /api/v2/daily-digest?date=2026-03-23`

```json
{
  "date": "2026-03-23",
  "summary_cn": "今日泰语区讨论焦点集中在充值系统异常（占消息量23%），用户反映iOS端充值后金额未到账……建议技术团队优先排查支付回调链路。",
  "metrics": {
    "total_messages": 14520,
    "thai_messages": 12300,
    "active_users": 342,
    "peak_hour": "15:00-16:00 (UTC+7)"
  },
  "top_topics": [...],
  "demand_signals": [...],
  "keyword_cloud": [...],
  "hourly_volumes": [...]
}
```

#### 6.3 新增 `GET /api/v2/trends?days=7`

提供过去 N 天的话题演变趋势：

```json
{
  "days": 7,
  "topic_trends": [
    {
      "topic_cn": "充值问题",
      "daily_counts": [12, 8, 45, 87, 120, 95, 60],
      "dates": ["03-17", "03-18", ...],
      "status": "爆发中"
    },
    ...
  ],
  "signal_trends": [
    {
      "signal_cn": "Bug报告",
      "daily_counts": [3, 5, 2, 15, 22, 18, 10],
      "status": "上升"
    },
    ...
  ]
}
```

#### 6.4 保留现有 v1 API 不动

所有现有 `/api/*` 端点保持不变，新端点放在 `/api/v2/*` 下，前端按需迁移。

---

### 改动 7：翻译服务模块 `src/aggregator/translator.py`

**独立的翻译服务**，封装所有"泰语→中文"的转换逻辑：

```python
class ThaiChineseTranslator:
    """缓存优先的泰中翻译服务"""

    def translate_keywords(self, keywords: list[str]) -> dict[str, str]:
        """批量翻译关键词，优先查缓存表"""

    def translate_summary(self, thai_summary: str) -> str:
        """将泰语/混合语摘要翻译为纯中文"""

    def name_topic(self, keywords_cn: list[str], sample_messages: list[str]) -> str:
        """为话题聚类生成中文标题"""

    def translate_demand_example(self, thai_text: str) -> str:
        """翻译需求信号的示例文本"""
```

**关键设计**：
- 翻译结果写入 `keyword_translations` 表，永久缓存
- 批量翻译：一次 LLM 调用翻译 20-30 个词（~300 tokens），远比逐词翻译高效
- 对于摘要翻译，使用较大的 `max_output_tokens`（800）确保完整输出

---

### 改动 8：异步分析任务

**现状**：`POST /api/analysis/run` 是同步的，大窗口分析可能要 30-60 秒。

**改进**：
1. 新增 `analysis_tasks` 表：
```python
class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/running/done/failed
    mode: Mapped[str] = mapped_column(String(20))
    progress: Mapped[int] = mapped_column(Integer, default=0)           # 0-100
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
```

2. API 变更：
- `POST /api/v2/analysis/run` → 立即返回 `{"task_id": "xxx"}`
- `GET /api/v2/analysis/tasks/{task_id}` → 查询进度 `{"status": "running", "progress": 65}`
- 前端轮询进度，展示进度条

3. 后端用 `threading.Thread` 或 `concurrent.futures.ThreadPoolExecutor` 在后台执行分析

---

## 四、前后端职责分工

### 后端负责（本方案核心）

| 职责 | 说明 |
|------|------|
| 消息采集与清洗 | 现有 Collector，增加去重和质量评分 |
| 小时级聚合 | TF-IDF + 话题聚类 + 需求信号提取 |
| 泰→中翻译 | 关键词缓存翻译 + 摘要翻译 + 话题命名 |
| 每日 Digest | 汇总小时数据，生成日级中文报告 |
| 趋势计算 | 话题/信号的多日演变数据 |
| 结构化 API | 返回前端可直接渲染的中文 JSON |
| 异步任务 | 长耗时分析后台执行，支持进度查询 |

### 前端负责（能力有限，保持简单）

| 职责 | 说明 |
|------|------|
| Dashboard 渲染 | 消费 `/api/v2/dashboard` 一个端点绘制全部卡片 |
| 词云展示 | 消费 `keyword_cloud` 数组，用 CSS/Canvas 绘制（或直接用列表） |
| 时间线图表 | 消费 `hourly_volumes` 画消息量柱状图 |
| 话题卡片 | 消费 `top_topics` 展示话题组 + 热度标签 |
| 每日报告页 | 消费 `/api/v2/daily-digest` 展示中文日报 |
| 趋势页 | 消费 `/api/v2/trends` 画折线图 |
| LLM 配置 | 现有功能，保持不变 |
| 分析触发 | 调 POST 接口后轮询任务状态，展示进度 |

---

## 五、实施优先级

按价值/成本排序：

| 优先级 | 改动 | 原因 |
|--------|------|------|
| **P0** | 改动 7: 翻译服务 + 改动 1: 翻译缓存表 | **不做这个，整个产品对中文用户等于不可用** |
| **P0** | 改动 6.1: `/api/v2/dashboard` | 前端需要结构化中文数据才能展示 |
| **P1** | 改动 3: 每日摘要 + 改动 6.2 | "今天聊了什么"是最核心用例 |
| **P1** | 改动 4: 增强需求信号 | 低成本高回报，只是加规则 |
| **P2** | 改动 2: 话题聚类 | 让数据更结构化，但需要调优 |
| **P2** | 改动 5: 消息去重 | 降成本，但不影响功能 |
| **P2** | 改动 8: 异步任务 | 改善体验，但不影响核心功能 |
| **P3** | 改动 6.3: 趋势 API | 锦上添花，依赖多日数据积累 |

---

## 六、数据库变更汇总

新增 3 张表：
1. `keyword_translations` — 泰中翻译缓存
2. `daily_digests` — 每日中文摘要
3. `analysis_tasks` — 异步任务队列

修改 1 张表：
4. `messages` — 增加 `content_hash`, `is_duplicate`, `quality_score` 字段

---

## 七、LLM 成本估算（每日）

| 用途 | 调用频率 | 每次 tokens | 日成本（DeepSeek） |
|------|----------|-------------|-------------------|
| 关键词翻译 | ~24次/天 × ~10新词 | ~300 | ~$0.01 |
| 话题命名 | ~24次/天 × 3-5组 | ~200 | ~$0.01 |
| 小时摘要 | ~24次/天 × 3-6块 | ~1200 | ~$0.05 |
| 每日 Digest | 1次/天 | ~2000 | ~$0.003 |
| 需求信号翻译 | ~24次/天 | ~200 | ~$0.01 |
| **合计** | | | **~$0.08/天 ≈ $2.4/月** |

使用 DeepSeek 作为默认 provider 时成本极低。OpenAI GPT-4.1-mini 约为 5-10 倍。

---

## 八、不改动的部分

- Collector 核心收集逻辑（`on_message` 等）— 已经很好
- 数据库连接层 (`db.py` 的 session 管理) — 稳定
- LLM provider 管理 API — 功能完整
- 加密存储 (`secrets.py`) — 够用
- 合规删除 API — 满足要求
- 配置加载 (`config.py`) — 灵活
