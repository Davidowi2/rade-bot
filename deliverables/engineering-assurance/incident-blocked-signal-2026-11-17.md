# Why the mirror keeps blocking valid signals — incident + full regression sweep

**日期**：2026-11-17
**工作流**：工作流 3（事故响应）+ 工作流 1（综合代码审查）
**参与成员**：Cody（代码审查师）/ Rex（SRE 工程师）/ Tessa（测试专家）

> **状态：已修复并推送（commit `c30f5d5`）。**
> 根因由线上告警确认：`[BLOCKED] quote timestamp is in the future beyond tolerance`，`source: catchup`——正是 F1。
> 已修：F1（慢读不再当作时钟异常）、F2（provider 异常进入 reason 与日志）、F4（删除 catchup 特例）、F13（`stale` 迁移 + 兜底）、`DEFAULT_SIGNAL_MAX_AGE_S` 120→900。
> **仍需人工执行一次：** 在 Supabase 上运行 `supabase/migrations/0006_allow_stale_status.sql`。
> 未动：F3（bid/ask 非原子）、F5（review 占额度）、F6（事件循环阻塞 I/O）、F7（`MIRROR_EXECUTE` 双源）——见 §9。

---

## 📌 TL;DR（执行摘要）

- **There are two independent defects, and both are silent.**
  1. **A `stale` refusal cannot be saved.** `mirror_pipeline.STATUS_STALE = "stale"` (`mirror_pipeline.py:53`) is written to `mirror_events`, but the Postgres CHECK constraint (`supabase/migrations/0001_mirror_events.sql:32-42`) does **not** include `'stale'`. No migration ever adds it (verified: zero occurrences of "stale" under `supabase/`). So every stale signal is rejected by the database, recorded as an *error*, and retried forever — **it never appears in the dashboard at all.**
  2. **A slow quote read is reported as a clock problem.** The quote is timestamped *after* the message arrived, then compared against a 2-second budget. Any broker read slower than 2.0s is refused with *"quote timestamp is in the future beyond tolerance"* — a message that sends you hunting a clock bug that does not exist. Reproduced through the real `evaluate_message`.
- **Why the previous fixes did not hold:** the local SQLite ledger has **no status constraint**, so every test and every local run happily writes `stale`. Production rejects it. The suite has never exercised the production schema.
- **Severity distribution**: 🔴严重 3 项 / 🟠高 4 项 / 🟡中 4 项 / 🟢低 2 项
- **阻塞 / 非阻塞**：both #1 and #2 are blocking for trusting the bot again. Everything else is not.

> **What I could not do:** the Render log stream is not in this repository and `/api/status` needs a bearer token I do not have. So the *exact* reason for last night's specific block is still unconfirmed. Everything below is either verified in code, reproduced locally, or explicitly marked as inference. §5 is the 60-second check that settles it.

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| 整体评级 | 🔴 不通过（"已修好"这个结论不成立） |
| 阻塞项数量 | 3 |
| 关键行动项 | 7 |
| 建议下一步 | 先看那一行到底存不存在（§5），存在就看 `reason`，不存在就是 F13 |

---

## 1. 事故时间线（observed vs inferred）

| 时间 (UTC) | 事件 | 来源 | 性质 |
|---|---|---|---|
| 2026-11-16 17:48 | commit `1719bcc` — 年龄 120→900s、风险上限 25→30、滑点 2.0→3.0、风控改用发布价 | git | 已观测 |
| 2026-11-16 21:16:55 | commit `21adf60` 推送到 `origin/main` | git | 已观测 |
| 2026-11-16 **21:18** | **服务重启**（由 `/healthz` 的 `uptime_s=9927.8` 反推，采集于 00:04Z） | 线上探针 | 已观测 |
| 2026-11-16 23:xx | 频道发出信号；约 1 分钟后被记为 blocked | 用户报告 | 已观测（时间由用户给出） |
| 2026-11-17 00:04 | `/healthz` 200，listener healthy，heartbeat 2.8s | 线上探针 | 已观测 |
| — | 事故时服务**处于唤醒状态**，运行的是当前代码（21adf60） | 由 uptime 推断 | 推断（高置信） |

**排除了什么：** 服务 21:18Z 重启后连续存活 2h45m，说明事故时**不是**在 free-tier 休眠。`docs/29` 记录的"睡了 33 分钟"那类漏单不是本次原因。信号是被收到之后、在评估或入库阶段失败的。

---

## 2. 最严重的问题：`stale` 状态根本存不进去

### 2.1 事实链

| 环节 | 位置 | 内容 |
|---|---|---|
| 代码定义 | `mirror_pipeline.py:53` | `STATUS_STALE = "stale"` |
| 返回点 | `mirror_pipeline.py:563` | `return _result(key, STATUS_STALE, stale, ...)` |
| 入库 | `store.py:105-125` | `claim()` → `POST mirror_events`，`status = "stale"` |
| 生产约束 | `supabase/migrations/0001_mirror_events.sql:32-42` | `status in ('paper_intent','ignored','manual_review','blocked','skipped_minimum_lot','duplicate','conflict')` — **没有 `stale`** |
| 迁移 | `supabase/migrations/000{2..5}` | **零次**出现 "stale"（已全目录 grep 确认） |
| 本地库 | `mirror_pipeline.py:915` `_migrate()` | SQLite **无任何 status 约束**，写什么都行 |

> **一个必须说清楚的保留意见。** 上述差异在**仓库里**是确凿的。但它是否在**线上库**里生效，取决于一件事：`0001` 用的是 `create table if not exists`。如果 `mirror_events` 在约束写进这个文件之前就已经建好，那么这条 CHECK **从未被应用过**，线上可能根本没有约束（那样 `stale` 就能写进去，F13 只是潜在风险）。用一条 SQL 就能判定，见 §5 第二步。

### 2.2 后果

`POST` 返回 400 → `StorageError` (`store.py:90-92`) → 冒泡到 `listener._process_safely()` → 记为 `status: "error"` → 该消息**不被 claim** → `gap_fill()` 把游标压回这条消息之前 (`listener.py:470-476`)。

于是每 15 秒：

1. 重新拉到这条消息 → 重新判定 stale → 重新 400；
2. 游标永远停在它前面，后面的消息每次都要重放；
3. 每次重放都重新取价（每条市价信号一次券商 HTTP 调用）；
4. 看板里**一条记录都没有**，只有 `/healthz` 里的 `errors` 计数在涨。

**这就是"静默"的字面含义**：不是被拒了，是**根本没被记下来**。你看到的"什么都没发生"，在数据库里就是什么都没发生。

### 2.3 为什么本地/测试全绿

本地跑的是 SQLite（`MIRROR_SQLITE_PATH`），生产跑的是 Supabase。约束只存在于生产侧。所以 18 个测试文件、81 个提交、连续 10 个"fix"提交，全部无法发现它——**测试从未跑过生产 schema**。

> 附带风险：`MIRROR_REQUIRE_DURABLE_STORE=0` 意味着这个差异只在部署后才显形。

---

## 3. 第二个问题：2 秒预算用在了错误的量上

### 3.1 机制

`process_message()` 先建信封（`received_at = now`，`listener.py:182`），**然后**才取价（`listener.py:194`）。两个 provider 都用 `datetime.now()` 打时间戳（`quotes.py:41`、`gateway.py:328`）。所以 `quoted_at` **永远晚于** `received_at`，差值就是这次取价的耗时：

```python
age = (as_utc(message.received_at) - as_utc(quote.quoted_at)).total_seconds()
if age < -max_age:      # max_age = MIRROR_QUOTE_MAX_AGE_S = 2.0
    return ... "quote timestamp is in the future beyond tolerance"
```

取价 > 2.0s 即拒。**Cody 用真实 `evaluate_message` 跑出来的结果：**

```
T+2.0s -> paper_intent  (passes)
T+2.5s -> blocked       reason: "quote timestamp is in the future beyond tolerance"
T+45.0s-> blocked       reason: "quote timestamp is in the future beyond tolerance"
```

**一次 45 秒的券商卡顿，被报告成"时钟异常"。** 这条信息不只是不完整，它把排查方向指错了。

### 3.2 为什么是间歇性

取价是**两次独立 HTTP 往返**（先 bid 再 ask：`quotes.py:39-40`、`gateway.py:324-325`）。正常 200–800ms 不触发；以下情况会偶发越线：冷连接/TLS 重连、令牌过期后的隐式重登、券商延迟尖峰、以及前面一条消息的阻塞式券商调用把线程拖住（F6）。这正好解释了"大多数时候好、偶尔失败、重试又好了"。

### 3.3 交叉盘：同一原因的第二张脸

两次独立调用之间市场跳动，就可能拿到 `bid > ask`，触发 `mirror_pipeline.py:609-617` 的 `"broker quote is invalid (bid=…, ask=…)"`。`docs/28` 已经记录过生产环境真的出现过（bid 3888.26 > ask 3888.09）。**这个"无效报价"是机器人自己造出来的。**

---

## 4. 排序后的候选原因（针对 11-16 夜间那一次）

| 排名 | 原因 | 机制 | 置信度 | 判别特征 |
|---|---|---|---|---|
| 1 | **慢取价 / 交叉盘**（F1+F3） | §3 | 高 | 有行，reason 含 `future beyond tolerance` 或 `quote is invalid` |
| 2 | **provider 异常被吞**（F2） | 令牌过期/网络 → `quote=None` | 高 | 有行，reason = `market signal needs a fresh broker quote` |
| 3 | **`stale` 无法入库**（F13） | §2 | 中高 | **没有行**，Render 日志反复 400 |
| 4 | 已穿越止损 | 1 分钟延迟 + 金价波动 | 中 | 有行，`market has already crossed the published stop` |
| 5 | 超出风险上限 | 部署值仍是 $25 而止损 >12.5 点 | 中低 | 有行，`exceeds the $25.00 per-trade ceiling` |
| 6 | 挂起 review 占位 | 旧 trade 卡在 review，永久占住 1 个仓位额度 | 低（一旦发生即永久） | 有行，`1 positions are already open and the limit is 1` |

**#1 和 #2 在同一条代码路径上**，这解释了为什么逐条打补丁总治不好：症状换了个字符串，病根没动。

---

## 5. 60 秒确证（唯一能定案的一步）

### 第一步：那一行到底存不存在？

**看板**：Dashboard → Events 表 → 按时间找 23:xx 的信号。
**或 API**（用 Render 里的 `MIRROR_API_TOKEN`）：

```bash
curl -s -H "Authorization: Bearer $MIRROR_API_TOKEN" \
  "https://rade-bot.onrender.com/api/events?limit=30"
```

**或 Supabase SQL：**

```sql
select created_at, message_id, status, source, reason, posted_at, received_at
from mirror_events
where created_at > now() - interval '24 hours'
order by created_at desc limit 30;
```

**判读表：**

| 观察 | 结论 | 去修 |
|---|---|---|
| 有行，reason 含 `future beyond tolerance` | F1 慢取价 | §9 #3 + #4 |
| 有行，reason = `needs a fresh broker quote` | F2 provider 异常被吞 | §9 #3 |
| 有行，reason 含 `quote is invalid (bid=…, ask=…)` | F3 交叉盘 | §9 #5 |
| 有行，reason 含 `crossed the published stop` | 延迟导致，非代码 bug | §9 #1 |
| **完全没有这一行** | 先查约束（第二步），可能是 **F13** | §9 #2 |

### 第二步：如果没行，先确认线上到底有没有那条约束

```sql
select conname, pg_get_constraintdef(oid) as definition
from pg_constraint
where conrelid = 'public.mirror_events'::regclass
  and contype = 'c';
```

- 返回里**没有** `mirror_events_status_valid` → 线上根本没约束，F13 不成立，回去看 F1/F2。
- 有，且定义里**不含** `'stale'` → **F13 确认**，`stale` 信号永远写不进去。

### 第三步：去 Render 日志搜这一条

```
could not process id=
```

配合 `400` / `violates check constraint` / `mirror_events_status_valid`。看到它，F13 即确认。

### 第四步：顺手量一下真实投递延迟

```sql
select posted_at, received_at,
       extract(epoch from (received_at - posted_at)) as delivery_lag_s
from mirror_events
where created_at > now() - interval '7 days'
order by created_at desc limit 50;
```

`delivery_lag_s` 是"发出→进入流水线"的真实延迟。`docs/29` 记录过 33 分钟的极端值；若这次约 60s，说明推送没到、走的是 15s 轮询，而 60s 意味着轮询本身被阻塞过（F6）。

---

## 6. 发现清单（按严重度）

| # | 严重度 | 类别 | 位置 | 问题 | 修复 |
|---|---|---|---|---|---|
| F13 | 🔴严重 | 数据完整性 | `0001_mirror_events.sql:32-42` vs `mirror_pipeline.py:53` | `stale` 不在 CHECK 约束里，stale 信号 400 失败 → 无限重试 → 看板无记录 | 加 `'stale'` 的迁移 |
| F1 | 🔴严重 | 正确性 | `mirror_pipeline.py:622-628` | 取价耗时被当成"时间戳在未来"，>2.0s 即拒，拒因误导 | 区分"读得慢"与"时钟异常" |
| F2 | 🔴严重 | 可观测性 | `listener.py:196,226,232-235` | provider 异常捕获后丢弃：不进日志、不入库 | `quote_error` 写入日志行与 `reason` |
| F3 | 🟠高 | 正确性 | `quotes.py:39-41`、`gateway.py:324-325` | bid/ask 两次独立调用可造出交叉盘 | 单次取价 / 交叉重试一次 |
| F4 | 🟠高 | 正确性 | `mirror_pipeline.py:385-394` | catchup 特例在 900s 下是空操作，却把所有轮询投递都算 catchup | 删除特例 |
| F5 | 🟠高 | 正确性 | `engine.py:91-102, 811-822` | `review` 永久占仓位额度；一条卡住的 review 永久阻断后续信号 | review 不占额度 / 显式告警 |
| F6 | 🟠高 | 性能 | `listener.py:194` | 阻塞式券商 HTTP 跑在 asyncio 事件循环里，拖慢整个摄入 | `run_in_executor` |
| F7 | 🟡中 | 配置 | `render.yaml:180` vs `update_render_env.py:74` | `MIRROR_EXECUTE` 一处 0 一处 1 | 单一来源 |
| F8 | 🟡中 | 行为/文档不符 | `mirror_pipeline.py:557-559` vs `:657-660, :758` | 注释与文档都写"LIMIT 调用从不复制"，但代码把 limit 信号判为 `paper_intent` 并带 `order_type="limit"`——执行开启时会真的挂单。政策与实现相反。 | 二者取一：改注释与文档，或真的拒绝 limit |
| F14 | 🟡中 | 可观测性 | `mirror_pipeline.py:385` + `service.py:434` | 容差元组里 `telegram_catchup`/`telegram_fetch` **零发射点**，全靠 `"catchup"` 恰好排在第三位；`/api/status` 报告"容差生效"，但它校验的是配置而不是代码是否认这个字符串 | 用常量替代字面量，并加契约测试 |
| F15 | 🟡中 | 数据 | `store.py:183` | `counts()` 取 5000 行**无序**样本在客户端计数，超过后静默少算，且样本不是最近的 5000 行 | 改为服务端聚合 |
| F9 | 🟡中 | 正确性 | `engine.py:_verify_order_size` | 用市价校验下单量，与 `mp:668` 不一致 | 统一用发布价 |
| F10 | 🟡中 | 可观测性 | `/healthz` | 只反映心跳，"健康但拒绝一切"仍是 healthy | 加最近拒因 + 24h 计数 |
| F11 | 🟢低 | 配置 | `render.yaml:36` | `plan: free`，休眠是漏单的结构性来源 | Starter 或常驻 ping |
| F12 | 🟢低 | 文档 | `docs/29` | 仍写"120s / $25"，与当前值不符 | 更新或标注过时 |

---

## 7. 配置漂移

同一设置在四个地方各有一份真相，且已经不一致：

| 设置 | render.yaml | update_render_env.py | .env.example | 判定 |
|---|---|---|---|---|
| `MIRROR_EXECUTE` | `"0"` (L180) | `"1"` (L74) | `0` (L29) | **DRIFT — 致命** |
| `MIRROR_MAX_TRADE_RISK_USD` | `"30"` | `"30"` | `25` (L31) | DRIFT |
| `MIRROR_MAX_SLIPPAGE` | `"3.0"` | `"3.0"` | `2.0` (L48) | DRIFT |
| `MIRROR_QUOTE_PROVIDER` | `tradelocker` | `tradelocker` | `none` (L74) | DRIFT（本地有意） |
| `MIRROR_RUN_LISTENER` | `"1"` | `"1"` | `0` (L76) | DRIFT（本地有意） |
| `MIRROR_MAX_SIGNAL_AGE_S` | `"900"` | `"900"` | 缺失 | 缺省 |
| `MIRROR_EQUITY` | **重复两次**（L68 `sync:false` / L122 `"4862.73"`） | `4862.73` | **重复两次**（L40 有值 / L73 空） | **重复键** |

两个具体风险：

1. **`MIRROR_EXECUTE` 双源**：`render.yaml` 说 0（纸面），`update_render_env.py` 说 1（真实下单）。谁最后跑谁赢——一次 Blueprint 同步就能静默关掉执行，或反过来静默打开真实下单。
2. **`MIRROR_EQUITY` 重复**：在 `.env.example` 里，`python-dotenv` 取**后者**，也就是空字符串 → `float("")` → 权益无效 → 本地跑必然全拒。`render.yaml` 里同名键出现两次，行为取决于解析器。

---

## 8. 测试为什么没拦住

**测试套件是绿的——这才是问题。**

实际执行（Tessa，装了 pytest 9.2.0 到默认解释器）：**531 collected / 519 passed / 6 failed / 6 skipped**。6 个 failure 全是缺第三方模块（telethon ×4、dotenv ×1、pandas ×1），不是逻辑失败；6 个 copier 关键文件单独跑 **187 passed, 0 failed**。

**套件全绿，bug 依然存在。** 原因不是覆盖不足，是**断言的对象不对**：

1. **没有任何测试断言"合取"。** 所有正向用例都用 `age = 0` 且报价时间戳恰好等于收信时间。`test_mirror_pipeline.py:22` 就是 `MessageEnvelope(..., NOW, NOW, ...)`，该文件 44 个测试因此让年龄检查**恒真**；`test_signal_age.py:52` 的 `fresh_quote()` 是 `MarketQuote(3915.0, 3915.0, NOW)`——零年龄、零点差。每次修 bug 都只加了一个**孤立阈值**测试，从没有一条断言"一条真实信号（60 秒前发出、报价晚 200ms、市价离发布价 0.3 点）能走到 `paper_intent`"。所以任何新阈值都可以把合取收紧到真实信号之下，而套件照样全绿。**这就是复发的机制。**
2. **测试断言"reason 存在"，而不是"原因被保留"。** `test_mirror_listener.py:150-157` 断言的是 `out["quote_error"]`——一个转瞬即逝的 dict 字段。用 `ConnectionError("TradeLocker session expired (401)")` 复现：status=blocked，落库的 reason 是 `market signal needs a fresh broker quote`，日志行一模一样，`quote_error` 被丢弃。**测试通过，诊断信息没了。** 这也是为什么上一次修复去动年龄守卫——年龄是唯一一个拒因里带数字的分支。
3. **`stale` 状态其实被测过。** `test_mirror_pipeline.py:148` 明确断言 `result.status == "stale"`。测试覆盖了"产生这个状态"，没覆盖"这个状态能不能落库"。
4. **2026-11-16 新加的 catchup 分支零测试。** `mirror_pipeline.py:385-394` 没有用例；唯一名字含 catch_up 的 `test_signal_age.py:176` 用的是 `envelope_from_telegram` 默认 `source="telegram_live"`，走的是 live 分支。把 `listener.py:460` 或 `mp:385` 任一字面量改名，所有轮询投递的信号会静默退回 120s 限制，**套件仍然全绿**。
5. **冻结行情既没测也测不出。** 探针：bid/ask 完全不变、`quoted_at` 持续推进 → 返回 `paper_intent`。滑点是"报价 vs 发布价"，所以一条冻结在入场价上的行情，会永远读成 0 滑点。
6. **本地用 SQLite，生产用 Postgres。** `_migrate()` 建的表对 `status` 无约束，`stale` 在测试里写得进去。**生产 schema 从未进入测试路径。** 只有 2 个测试文件提到 supabase，且都不解析 `supabase/migrations/*.sql`。
7. **没有一条端到端测试**：从"频道发一条有效信号"到"券商收到一张单"。所有测试都停在 `evaluate_message` 的返回值上。

**P0 回归测试（必须新增）：**

| # | 测试 | 钉住什么 | 现状 |
|---|---|---|---|
| T0 | `test_a_realistic_live_signal_reaches_paper_intent` | 60s 前发出 + 报价晚 0.2s + 离发布价 0.3 点 + 10 点止损 → 必须 `paper_intent` | **这就是本次事故的原样场景，从来没写过** |
| T1 | `test_stale_status_is_accepted_by_production_schema` | 解析 `supabase/migrations/*.sql` 的 CHECK 集合，断言全部 `STATUS_*` 合法（**不需要连数据库**） | 缺失 |
| T2 | `test_slow_quote_read_is_reported_as_slow` | 2.0s 通过 / 2.1s 拒绝，reason 必须含"took"，**不得**含"future" | 缺失 |
| T3 | `test_quote_provider_error_reaches_ledger_and_log` | provider 抛 401 时，异常文本出现在可落库的位置 | 缺失（现有用例只断言 dict 字段） |
| T4 | `test_a_frozen_feed_is_detected` | bid/ask 不变 + 时间戳前进 → 必须报冻结 | 缺失，**当前返回 `paper_intent`** |
| T5 | `test_the_catchup_source_string_is_the_one_the_pipeline_honours` | `listener.py:460` 的字面量必须出现在 `mp:385` 的元组里 | 缺失 |

---

## 9. 行动清单

| # | 行动 | 负责角色 | 紧急度 | 预期完成 |
|---|---|---|---|---|
| 1 | 按 §5 判读，定案是"有行"还是"无行" | 你 | P0 | 今天，2 分钟 |
| 2 | 加 `'stale'` 迁移并部署（F13） | Rex | P0 | 今天 |
| 3 | 合并 F1 + F2 补丁（见附录 A） | Cody | P0 | 今天 |
| 4 | 临时把 `MIRROR_QUOTE_MAX_AGE_S` 从 2 提到 10（立即缓解，见 §10） | 你 | P0 | 今天 |
| 5 | 取价改单次调用或交叉重试（F3） | Cody | P1 | 本周 |
| 6 | 补 T1/T2/T3 三个回归测试 | Tessa | P1 | 本周 |
| 7 | 统一 `MIRROR_EXECUTE` 与 `MIRROR_EQUITY` 的单一来源（F7） | Rex | P1 | 本周 |

---

## 10. 立即缓解（不等代码）

在 Render 环境里把 **`MIRROR_QUOTE_MAX_AGE_S` 从 `2` 改成 `10`**，然后重启。

理由：这个值现在的语义是"一次取价最多允许花多久"。10 秒对黄金、对 10 点止损、对已有 3.0 点滑点保护来说是可接受的；2 秒对两次跨网 HTTP 往返来说不合理。**这会让大部分 F1 拦截立刻消失**，而且不需要发版。

同时请注意：这个值**不能**治 F13。如果信号是 stale 入库失败，改它没有用——那必须先跑迁移。

---

## 11. 已知局限

- **没有 Render 日志流**。本报告不含任何生产日志原文；所有"日志里会看到什么"都是按代码路径推导的。
- 未能确认部署环境里 `MIRROR_MAX_TRADE_RISK_USD` 究竟是 25 还是 30。
- 测试套件已实际执行（531 collected / 519 passed / 6 failed / 6 skipped），6 个失败均为缺少 telethon / dotenv / pandas，非逻辑失败；copier 关键 6 个文件单独跑 187 passed / 0 failed。**套件是绿的，问题不在这里。**
- 概率是判断，不是测量。

---

## 12. 数据来源

- **Cody（代码审查师）**：分支全图、F1–F9、F1 真实路径复现（2.0s/2.5s/45s）、五个补丁草案（附录 A）、P0 测试规格
- **Rex（SRE）**：时间线、SEV-2、配置漂移、`stale` 约束缺口、F14/F15、提取方案与 SQL、对 F2 方案的修正（不要把异常文本写进 `reason`）
- **Tessa（测试专家）**：实际执行套件（531/519/6/6）、33 条分支的覆盖矩阵、7 条结构诊断、P0–P2 计划；并**纠正了本报告初稿中关于 LIMIT 信号的一处错误结论**（初稿称"编辑检查先于 limit 规则会导致重复执行"，实际是代码复制 LIMIT 信号而其注释声称从不复制 → 见 F8）
- **本报告独立复核**：F1 数值复现、`quote_error` 全仓 grep 无持久化、`POSITION_STATES` 含 review、`MIRROR_EXECUTE` 双源、`supabase/` 全目录无 "stale"、LIMIT 分支实际返回 `paper_intent`（mp:657-660, 758）

---

## 附录 A：五个补丁草案（text only，未应用）

```diff
--- a/src/rade/observer/mirror_pipeline.py
+++ b/src/rade/observer/mirror_pipeline.py
@@ -619,15 +619,20 @@
-        max_age = float(risk.quote_max_age_seconds)
-        age = (as_utc(message.received_at) - as_utc(quote.quoted_at)).total_seconds()
-        if age < -max_age:
-            return _result(key, STATUS_BLOCKED,
-                           "quote timestamp is in the future beyond tolerance", signal=parsed)
-        if age > max_age:
-            return _result(key, STATUS_BLOCKED, "broker quote is stale", signal=parsed)
+        max_age = float(risk.quote_max_age_seconds)
+        age = (as_utc(message.received_at) - as_utc(quote.quoted_at)).total_seconds()
+        if age < 0:
+            # The quote was taken after the message arrived: `age` is the read
+            # latency, not a clock disagreement. Only a genuinely future
+            # timestamp (beyond a skew tolerance) is a clock problem.
+            read_s = -age
+            if read_s > max_age:
+                return _result(key, STATUS_BLOCKED,
+                               f"reading the broker quote took {read_s:.1f}s, "
+                               f"beyond the {max_age:g}s budget "
+                               "(MIRROR_QUOTE_MAX_AGE_S); the price the signal "
+                               "was sized against is not current", signal=parsed)
+        elif age > max_age:
+            return _result(key, STATUS_BLOCKED, "broker quote is stale", signal=parsed)
```

```diff
--- a/src/rade/observer/listener.py
+++ b/src/rade/observer/listener.py
@@ -229,7 +229,7 @@
-        self.log(
-            f"{source} id={mid} rev={revision} -> {result.status}"
-            + (f" ({result.reason})" if result.status != "paper_intent" else "")
-        )
+        self.log(
+            f"{source} id={mid} rev={revision} -> {result.status}"
+            + (f" ({result.reason})" if result.status != "paper_intent" else "")
+            + (f" [quote_error: {quote_error}]" if quote_error else "")
+        )
```

> 剩余三个（F7 review 占额度、F5 catchup 特例、F4 下单量校验）的完整 hunk 见 Cody 的原始产出。三个都标了 **RISK：需在 demo 账户上实测后再上线**。

> **对 F2 方案的一处修正（Rex 提出，已采纳）：** 不要把异常文本塞进 `mirror_events.reason`。该列现在是低基数的，可以直接 `group by reason` 做拒因统计；一旦写入自由文本，每行都唯一，未来的拒因分析就废了。正确做法是**新增一个 `diagnostic` 列**承载异常原文——而这又需要一次**真正被执行**的迁移，而 `stale` 那个约束缺口恰好证明"迁移会被执行"不是安全假设。所以顺序是：先确认线上 schema 现状（§5 第二步），再决定迁移怎么写。

---

> 本报告由工程保障团队 AI 协作生成，关键决策请由人类工程负责人复核。
