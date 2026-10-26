# 测试套件提速：可优化点清单（基于实测）

> 对象：`tests/` 全量 pytest 套件（605 个用例，真实 `dolt` 二进制，无 mock）。
> 测量环境：Apple M1 Max（10 核 / 8 性能核），macOS，`dolt 2.3.5`，Python 3.11.15，
> `pytest 9.1.1 + pytest-xdist 3.8.0`，`pyproject.toml` 的 `addopts = -ra -n auto --dist loadfile`。
> 数字标注：**【实测】**=本次跑出来的数字；**【静态】**=由代码/收集用例逐条数出；**【推算】**=明确写出假设。
> 详细证据（逐条 file:line、复现脚本、原始日志）见仓库 `perf-notes/` 三份审计笔记（未提交，随本报告留存）。

---

## 0. 一页结论（TL;DR）

1. **基线：全量 605 用例、605 passed，墙钟 758.27s（12:38）**，`user 584s + sys 785s = 1369s` CPU。
   其中 **406 个纯 Python 用例合计 < 2s**；**199 个碰真 dolt 的用例吃掉 > 99% 的时间**。
2. **根因只有一个：每次 SQL 都起一个 `dolt sql` 子进程。** 一次全量运行约 **1.0–1.3 万次 dolt 子进程**
   （完整 session 实测 **13,072 次：DDL 5,813 / SELECT 3,335 / csv 二次探测 2,450 / flush 619 / commit 411 /
   `dolt init` 158**，平均 21.6 次/用例）【实测】。单次调用空闲 ~60–100ms，10 个 worker 并发的实测均摊
   **~0.24–0.39s/次**。11 分钟基本就是「调用次数 × 单次成本」。
3. **`--dist loadfile` 把长尾放大成墙钟**：`test_schema_diff.py` 一个文件的 worker 时间 **757.4s ≈ 全量墙钟 758.3s**
   —— 它被整文件派给一个 worker，其余 9 个 worker 提前空闲。平均并行度只有 5.7x（CPU 等效占用约 1.8 核）。
   用基线数据做调度模拟：只拆 `test_schema_diff` + `test_docs_commands` 两个文件，墙钟即可 **757→541s（-29%）**，
   拆到每单元 ≤100s 可到 **440s**（理论下界 432s）。
4. **实测过的最大组合收益**：
   - 只换派发（干净环境，原代码，`--dist load`）：**758.27s → 579.07s（9:39，-24%），605 全过**；
   - 再叠加 3 个低风险产品补丁（DDL 批量化 + init 跳过 `refresh_meta` + `read_meta` 单次探测）：
     **605 passed / 385.35s（6:25），-49%**；worker 时间 **4323s → 2876s（-33%）**
     （该原型**不含** schema 内省批量化，仍有约 400s 的明显剩余空间）。
5. **收益天花板在「测试改走常驻 `dolt sql-server`」**：实测 server 单查询 **0.40ms**（embedded 是 2 次子进程 /
   ~0.15–0.2s）。若把 dolt 用例切到 server，理论 dolt 开销可压到 <1%，墙钟可到 **~1–2 分钟**；
   需保留 embedded 的专门覆盖，属测试基础设施改造。

### 如果只做一件事

> **把 `init_ledger` 里 34–67 条 DDL 合并成一次 `dolt sql` 执行**（产品代码 2–3 行，见 P0-1）。
> 单次 init(base) 实测 6.75s → 1.57s，套件里 108 次真实 ledger 初始化共 4996 次调用（占全部调用 42%），
> 仅这一项就能省约 900–1000 worker 秒，且 `tm init` 本身也变快。

### 三档路线图（数字为墙钟，含已验证部分）

| 档位 | 内容 | 预计墙钟 | 倍数 | 已验证程度 |
|---|---|---|---|---|
| 现状 | `-n auto --dist loadfile` | **758s（12:38）** | 1x | 实测 |
| A（半天内） | 拆重文件/调度 + P0-1/2/3（DDL 批量、跳过 refresh_meta、内省批量） | **~330–380s（5.5–6.5 分钟）** | ~2x | 其中 385s 已由原型实测（缺内省批量，带它应更低） |
| B（1–2 天） | A + 测试模板化 + 读路径 `query_many` + flush/commit 合并 | **~200–260s（3.5–4.5 分钟）** | ~3x | 各项已有独立原型/实测，未组合 |
| C（2–4 天） | B + 测试夹具改常驻 `dolt sql-server`（保留 embedded 覆盖） | **~60–120s（1–2 分钟）** | ~6–12x | server 单查询 0.40ms 已实测，夹具改造未做 |

---

## 1. 基线：时间到底花在哪

### 1.1 总账

| 指标 | 数值 | 说明 |
|---|---|---|
| 用例 | 605（19 个文件） | 406 纯 Python / 199 真 dolt |
| 墙钟 | **758.27s** | `-n auto --dist loadfile`【实测】 |
| CPU | user 584s + sys 785s = **1369s** | 大量 fork/exec + 文件 I/O |
| worker 时间合计 | **4323s** | 解析 `--durations=0` 的 call+setup+teardown，744 条 ≥5ms |
| 平均并行度 | **5.7x**（4323/758） | 10 个 worker；CPU 等效占用仅 ~1.8 核 → 瓶颈是等待而非算力 |
| dolt 子进程调用 | **~1.0–1.3 万次/全量** | 两次完整 session 记录 10,228 / 13,072 次；后者含 158 次 `dolt init` |
| 单次 dolt 调用成本 | 空闲 60–100ms；基线并发均摊 **0.238s** | 0.238s = schema_diff setup 221.24s ÷ 该文件 930 次 init 调用【基线反推】 |

> 注：基线那一次测量路径上刚好有我们放的计数 wrapper（每次调用多一个 shell/`time` 进程，约 +6–8ms/次），
> 按 10,228 次调用折算墙钟污染约 **10–15s**；即干净基线约 **745s**，与「11 分钟」的体感一致。
> 所有 wrapper 调用同时留下了全量调用分类，见 1.3。

### 1.2 逐文件 worker 时间（`--dist loadfile` 的调度单元）

| 文件 | 用例数 | call+setup+teardown（s） | 占 worker 时间 |
|---|---:|---:|---:|
| test_schema_diff.py | 31 | **757.4** | 17.5%（≈ 全量墙钟！） |
| test_docs_commands.py | 5 | 564.9 | 13.1% |
| test_read_side.py | 13 | 540.5 | 12.5% |
| test_validation.py | 18 | 499.7 | 11.6% |
| test_objects.py | 26 | 490.6 | 11.3% |
| test_ledger_init.py | 31 | 427.4 | 9.9% |
| test_csv_io.py | 15 | 359.3 | 8.3% |
| test_remote_backup.py | 13 | 268.4 | 6.2% |
| test_cli_objects.py | 6 | 154.7 | 3.6% |
| test_presets.py | 14 | 143.7 | 3.3% |
| test_ddl.py | 16 | 46.9 | 1.1% |
| 其余（driver_contract/workspace/cli_workspace/smoke） | 47 | 57.9 | 1.3% |
| **纯 Python 8 个文件**（schema_model/ids/schema_loader/driver_literals 等） | 406 | **~1.6** | ~0% |

**关键路径**：`test_schema_diff.py` 单文件 = 757.4s，几乎等于墙钟。`--dist loadfile` 以文件为调度单元，
这个 31 个用例的文件落到一个 worker 后，其他 worker 在大部分时间里无事可做。

### 1.3 dolt 子进程调用分类（完整 session 逐次记录）

一次 `--dist load` 全量 session 实测 **13,072 次 dolt 进程启动**（loadfile session 记录 10,228 次；
差异主要来自 load 模式下 session 模板在每个 worker 重建，以及 wrapper 晚挂载；量级按 **1.0–1.3 万次** 计）：

| 类别 | 次数 | 占比 | 说明 |
|---|---:|---:|---|
| DDL（CREATE / ALTER / DROP） | **5,813** | **44.5%** | 主要是 108 次 ledger 初始化 × 每 preset 34–67 条 DDL（每条 1 子进程） |
| SELECT（json 主查询） | 3,335 | 25.5% | |
| **SELECT（csv 列名二次探测）** | **2,450** | **18.7%** | 有结果行的读查询都要再跑一次 `-r csv` |
| 事务 flush / commit（START、CALL） | 619 + 411 | 7.9% | 每个写命令固定各 1 次 |
| `dolt init` | 158 | 1.2% | 每次 ledger 初始化 1 次 |
| `dolt config` 等其它 | 71 | 0.5% | `dolt_repo`/`make_ledger` 夹具的 2 次 config/测试 |
| **合计** | **13,072** | 100% | **平均 21.6 次进程启动/用例** |

**单用例极端值**：`test_docs_commands.py::test_command_reference_object_and_read_flows` 一条用例
= **761 次 dolt 进程 / 294.2s**（占全量墙钟 39%，因为它是「文档所有命令都跑一遍」的集成流）；
`test_migration_recipe...` 492 次、`test_schema_skill...` 434 次、`test_export_all_then_import...` 337 次。

### 1.4 单次操作触发的子进程数（产品代码现状，实测/静态）

| 操作 | 子进程数 | 组成 |
|---|---:|---|
| `init_ledger(base)` | **39** | `dolt init` 1 + **DDL 34 条各 1** + 事务 flush 1 + commit 1 + `refresh_meta` 2 |
| `init_ledger(full)` | **65** | 60 条 DDL + 5（同上） |
| `init_ledger(molbio)` / `(library)` | 72 / 54 | 67 / 49 条 DDL |
| `open()` | 2 + 1 | `read_meta` 的 json+csv 双调用 + `pgrep` 1 次子进程 |
| `entities.add` | 4 | next_id 查询 + alias 唯一性查询 + flush + commit |
| `boxes.add grid96` | 3 | 重名检查 + flush + commit（96 条 slot INSERT 被合并） |
| `aliquots.add` | 15 | 先做 13 次读校验（每次 1–2 调用）+ flush + commit |
| `find` / `history` / `export_csv(all)` / `import_csv(all)` | 21 / 18 / 19 / 21 | 读操作中约 29–50% 是「有结果行→再跑一次 `-r csv` 取列名」的探测 |
| `schema plan()`（`_introspect`） | 18–46 | 6 条 information_schema + 每个非核心表 1 条 `SHOW CREATE TABLE` + ledger_meta，**每条 2 次调用**（json+csv） |
| `test_schema_diff.py` 全文件 | **2,994 / 637s dolt 内部耗时** | 31 次 init（930）+ 约 99 次 plan 内省（1782）+ apply DDL（~282） |

> 读路径的「json + csv 二次探测」是 embedded driver 为了补齐 Dolt JSON 省略 NULL 列的权宜之计
> （`src/tetramark/driver/embedded.py:162-184`）。在保持 Driver 契约（列类型 + NULL 键齐全）前提下
> 没有单次调用替代方案（已实测排除 JSON_OBJECT/单次 CSV/vertical），可行的是把 N 条查询批成 2 次调用（P1-1）。

### 1.5 为什么 10 个 worker 只跑出 5.7x

- **不是 CPU 瓶颈**：1369s CPU / 758s 墙钟 ≈ 1.8 核；4/5 的时间在等进程启动、打开 Dolt 数据库、fsync/文件 I/O。
- **并发越高单次越慢**：load≈20 时实测单次调用 0.33–0.44s（空闲 0.06–0.1s），即 10 个 worker 互相把对方拖慢。
  因此**单纯换更细的 dispatch 收益有限**（干净实测 758→579s，-24%），必须减少调用次数。
- **单测地板**：最慢单测 `test_command_reference_object_and_read_flows` 基线 294s（load 下 327s），
  即使完美均衡，墙钟也不会低于它。

---

## 2. 可优化点清单

### 2.1 总览（按 ROI 排序）

| ID | 类型 | 改什么 | 预计收益 | 改动面 | 风险 | 优先级 | 验证状态 |
|---|---|---|---|---|---|---|---|
| **P0-1** | 产品 | `init_ledger` 的 DDL 循环改一次性执行 `plan.sql`；或在 EmbeddedDriver 里合并连续 DDL | init 39/65/72/54 → 6 次；单测 init 6.75s→1.57s；全量约 **-900~1000 worker 秒** | 2–3 行（`core/ledger.py:423-425`） | 低 | **P0** | 原型实测（含在 385s 组合里） |
| **P0-2** | 产品 | init 末尾跳过 `refresh_meta()`（meta 值已在内存） | init 6→4 次，1.57s→1.13s；108 次 init 省 216 次调用 | ~10 行 | 低 | **P0** | 原型实测 |
| **P0-3** | 产品 | `schema/diff.py::_introspect` 批量化（新增 `Driver.query_many`：embedded 用 1 次 json 批 + 1 次 csv 探测批） | `plan()` 18–46 → 2 次调用；`test_schema_diff.py` 2,994→~1,400 次、dolt 内部耗时 637→~450s（叠加 P0-1/2 后 ~150s） | ~80 行（driver 3 文件 + diff.py） | 中 | **P0** | 独立原型已验证 query_many 可行（9 查询 18→2 次，7.8x） |
| **P0-4** | 测试 | 把仍在用慢速 `ledger_factory` 的 84 个用例迁到模板复制 `fast_ledger_factory`（validation 18、schema_diff 30、read_side 13、remote_backup 13、presets 5、ledger_init 5；后者 13 个 init 语义用例不迁） | **未做 P0-1 时**：-2,800~3,000 次 dolt 进程（-22%）、~400 worker 秒；loadfile 调度模拟 **757→566s（-25%）**，与「拆文件」叠加 492s（-35%）；**P0-1 落地后**只剩 35–70 worker 秒（1.2–2.4%） | 仅 tests/（conftest 加变体 + 每文件 6 行 alias） | 低 | **P0（若不先做 P0-1）/ P2（若做 P0-1）** | 模拟+静态核查；已有 3 个文件在用的现成模式；需先修 fast 工厂的 preset 静默错配 |
| **P0-5** | 测试/配置 | 拆重文件（`test_schema_diff.py` 31→4 个文件、`test_docs_commands.py` 按流程拆）；或 `--dist load` + server 测试 `xdist_group` | 关键路径 757s 消失；`--dist load` **干净实测 758→579s（-24%），605 全过**；拆文件模拟 757→541s | 测试文件组织 / addopts | 中（server 测试对时序敏感） | **P0** | `--dist load` 实测；拆分已用基线数据模拟 |
| **P1-1** | 产品 | 读路径 `query_many`：`find`/`history`/`export`/`box show`/`import` 的多次读合并 | find 21→2、history 18→2、export 19→2、box show 6→2、aliquot add 读阶段 12→2；读调用 -60~90% | driver + ops 5 个文件（~200–400 行） | 中 | P1 | query_many 原型已实测 |
| **P1-2** | 产品 | 写操作把事务 flush 与 `DOLT_ADD/DOLT_COMMIT` 合进同一条 `dolt sql -q` | 每个写命令 **-1 次调用**（entity add 4→3、box 3→2…） | ~50 行 | 中（nothing-to-commit 语义） | P1 | 单点原型已验证（1 次调用返回 hash） |
| **P1-3** | 产品 | `Workspace.driver_for` 先探端口再 `pgrep`；`read_meta` 用 `query(columns=False)` 省 csv 探测 | 每次 open -1 次调用 + 省掉无 server 时的 1 次 `pgrep`；全量 ~260–320 次调用 | ~20 行 | 低 | P1 | 含在原型 385s 组合里 |
| **P1-4** | 测试 | 模板跨 worker 共享 + 懒构建变体；fast 复制跳过 `open`；`dolt_repo` 改用空 repo 模板；watchdog `sleep 1`→`sleep 5` | ~70–80 + 24 + 20–25 + 23–38s（CPU） | conftest 若干 | 低 | P1 | 静态/实测（copy 17ms） |
| **P1-5** | 产品 | 去冗余查询：uuid `next_id` 的全表 `LIKE '%'` 扫描、`show_*` 重复 fetch、import 规划器重复 resolve | 场景内 -6 次调用（~2%），大账本上还消除 O(n) 扫描 | 分散 ~50–80 行 | 低 | P1 | 静态 |
| **P2-1** | 测试/基础设施 | 测试夹具改常驻 `dolt sql-server`（session 级/每 worker 一个）+ ServerDriver；embedded 覆盖保留在 driver contract 等专用测试 | 每查询 0.40ms vs 2 次子进程；dolt 开销 → ~1%；墙钟目标 **~1–2 分钟** | tests/conftest + 少量测试（需设计 DB 隔离） | 中高 | P2（收益最大） | server 成本实测，夹具未改造 |
| **P2-2** | 产品 | embedded 驱动改「常驻 `dolt sql` 进程 + stdin 流式」 | 每语句 0.3–20ms | embedded.py 重写 ~200–300 行 | 高（任何 SQL 错误会杀死进程，需重启恢复） | P2 | 可行性原型已验证，不建议现在做 |
| **P2-3** | 流程 | 分层运行：纯 Python 4 个文件（341 个用例，合计 ~1.6s）日常单独跑；`test_docs_commands` 打 `docs` marker 单独 job；server 用例单独 job；CI 分片（当前仓库无任何 CI 配置） | 日常回路 11 分钟→秒级；全量不变 | pytest 标记/CI | 低 | P2 | 静态/实测 |
| **P2-4** | 调优 | `-n auto`=10 把 2 个 E 核也算进 worker，对 8 个性能核过订；单文件初测：`-n 4` 145.95s / `-n 6` 135.81s（CPU 279.6→297.1s），**4→6 只快 7% 但 CPU 反升 6%** | 建议 `-n 8`（本地）/ CI 按核数；n10 短实验被中断，未跑完 | addopts 1 行 | 低 | P2 | 初测（1 个文件） |
| **P2-5** | 杂项 | `uv run` 用 `--frozen` 或直接 `.venv/bin/python`（环境已同步时 uv 开销 ~亚秒级，不是主因）；tmp_path 清理（pytest 自动保留 3 次，1–2s）；watchdog 等 | 秒级 | — | 低 | P2 | — |

### 2.2 P0 细节与实测证据

#### P0-1 `init_ledger`：34–67 条 DDL 合并成一次执行

**现状**（`src/tetramark/core/ledger.py:423-425`）：

```python
with driver.transaction():
    for statement in plan.statements:
        driver.execute(statement.sql)   # 每条 CREATE/ALTER 命中 _DDL 分支 → 单独一次 dolt sql
```

**改法**（`InitPlan.sql` 已经存在，就是分号连接的 DDL 脚本）：

```python
with driver.transaction():
    driver.execute(plan.sql)            # 一次 dolt sql -q 跑完 34–67 条 DDL
```

**实测**（Dolt 2.3.5，真实 repo，逐变体验证 open + `check()` + fingerprint + `dolt_log`）：

| 变体 | init(base) 子进程 | init(base) 墙钟 |
|---|---:|---:|
| 现状（DDL 逐条） | 39 | 6.75s |
| DDL 合并 | 6 | 1.57s（-77%） |
| DDL 合并 + 跳过 init 收尾 `refresh_meta` | 4 | 1.13s（-83%） |
| 再把 DML+commit 也并进同一条 SQL | 2 | 0.74s（-89%） |

- 失败语义【实测】：批内某条 DDL 出错 → `exit=1` + `stderr: error on line N for query ...`，报错前已执行的 DDL
  已 autocommit 落地（与现在逐条执行完全一致），后续语句不执行；`init_ledger` 的异常清理（rmtree）语义不变。
  ⚠️ **不要加 `dolt sql -c/--continue`**：实测它会「出错后继续执行且 exit=0」，绕过 embedded driver 的错误检测。
- 纯 DDL 批量的独立实测：base 34 条 DDL 逐条 **34 次 / 14.5s**（子进程 CPU 2290ms）→ 一次批量 **1 次 / 1.35s**
  （CPU 122ms），**wall 10.8x / CPU 18.7x**，两种方式建出的 schema 逐对象一致。
- DDL 脚本大小 base 10KB / full 17KB / molbio 21KB，远低于 argv 限制。
- 套件影响：**108 次真实 ledger 初始化 = 4,996 次调用（占全部 42%）≈ 1,190 worker 秒**。
  P0-1+P0-2 之后降到 ~4 次/init，约 **-900~1000 worker 秒**。
- 附带收益：`tm init`（产品命令）同样从数秒降到亚秒级。

#### P0-2 init 末尾跳过 `refresh_meta`

`init_ledger` 在写完 meta 并 commit 后，又调 `ledger.refresh_meta()` 回读一次 `ledger_meta`（json+csv = 2 次调用）。
值刚刚由 `_write_meta` 写下去，直接放进内存即可。~10 行改动，单 init -2 次调用。

#### P0-3 schema 内省批量化（`_introspect`）

`plan()` 每次做 6 条 information_schema 查询 + 每个非核心表一条 `SHOW CREATE TABLE` + `ledger_meta`，
**每条有结果行的查询都是 2 次子进程**（json + csv 取列名），合计 `plan()` 18/24/34/42/46 次调用
（自建 preset/base/library/full/molbio）。`test_schema_diff.py` 平均 3.2 次 `plan()`/测试 → 1,782 次调用，
是该文件 2,994 次调用的 **60%**。

**最小改法**（风险最低，已验证思路）：

1. `driver/base.py` 加 `query_many(statements, *, columns=True)`，默认实现 = 顺序 `query()`（ServerDriver 自动满足）。
2. `driver/embedded.py` 覆写：`columns=False` 时用 **一条 `dolt sql -r json -q`** 批全部语句，按 `{}` 占位对齐结果集。
3. `_introspect` 改两批：6 条 information_schema + ledger_meta 一批；所有 `SHOW CREATE TABLE` 一批 → **共 2 次调用**。
4. 可空列由 `row["x"]` 改 `row.get("x")`（JSON 省略 NULL 列）。

注意：`SHOW CREATE TABLE` 不能仅用 information_schema 替代（Dolt 2.3.5 的 columns 不含 generated column 表达式，
而 diff 需要表达式文本来判断重建），所以必须批量化而不是删查询。

**已验证**【实测】：
- `_introspect` 真实 base ledger（6 条 information_schema + 5 条 `SHOW CREATE TABLE` + meta）：
  逐条+探测 **24 次子进程 / 10.5s** → 批量 **2 次 / 0.98s（-92%）**，结果逐行完全一致。
- `query_many` 通用原型：9 条查询 18 次子进程 4,069.7ms → **2 次子进程 520.8ms（7.8x）**，内容逐字段一致。
- 对 `test_schema_diff.py`（原型补丁后 656 worker 秒）预计再降 **~210–270s**（该文件 656→~390–450s）。

#### P0-4 测试夹具：扩大模板复制覆盖面

- 现状：`fast_ledger_factory`（session 级预建模板 + 每测试 `copytree`）只有 3 个文件 / 47 个用例在用；
  `ledger_factory`（每测试全新 `dolt init` + 全量 DDL）仍有 **93 个用例**。
- 实测成本：`init_ledger(base)` 5.44s（含 workspace 创建）；`fast_ledger_factory` 一次（copytree + open + close）**392ms**
  → **14x**；`shutil.copytree` 本身只要 **17.1ms**（模板 14 文件/170KB）。
- 可机械切换且已核查安全的文件（测试不依赖 `ledger.name/id_prefix/author`）：
  `test_validation.py`（18 次 full init）、`test_remote_backup.py`（13 次 base init）、`test_read_side.py`（13 次，需补 bundled-base 变体）。
  `test_schema_diff.py` 收益最高但需要「每测试重写 preset 路径 + reload schema」的 spike。
- 前置修复：`fast_ledger_factory`（`tests/conftest.py:279-281`）目前把除 base 外的任何 preset **静默映射到 full 模板**，
  传错 preset 会得到错误 schema 而不报错；转换前必须先改成显式变体映射 + 未知 preset 抛错。
- 不建议：APFS clonefile（`cp -c` 实测 25.8ms，反而比 copytree 17.1ms 慢）、`dolt backup`/`dolt clone` 替代复制（都更慢）。
- **优先级随 P0-1 变化**：DDL 批量落地后，所有 init 从 2–5s 降到 ~1s 以内，模板化的剩余收益只有 35–70 worker 秒
  （1.2–2.4%），此时该方案降级为 P2；**仍建议先做前置修复**（正确性隐患），但不值得为性能大改夹具。
  `dolt reset --hard` 共享 ledger 的方案实测是 no-op（reset 到 HEAD 不会撤销工作区新表/行），不可行。

#### P0-5 调度：拆分重文件 / 换 dispatch

| 做法 | 实测/推算 | 备注 |
|---|---|---|
| `--dist loadfile`（现状） | 758s | 单文件 757s 决定墙钟 |
| `--dist load`（逐用例动态派发，1 行配置） | **干净环境实测 758.27s → 579.07s（-24%），605 全过**；污染环境 612.88s + 3 个 server 失败是 wrapper 假象 | 模板 workspace 会在更多 worker 重建（+312 次 dolt 进程）；server 用例对时序敏感，建议加 `xdist_group` 兜底；需一次完整验证 |
| **物理拆文件（推荐）**：`test_schema_diff.py`→4 个模块 + `test_docs_commands.py`→5 个模块，保留 loadfile | 调度模拟（用基线 per-test 时长，模拟器复现基线误差 0.1%）：**757s → 541s（-29%）**；继续拆 >400s 的文件 → 499s；拆到每单元 ≤100s → **440s ≈ 理论下界 432s** | 零测试语义变化，只搬家 + 共享 helper 模块 |
| `--dist worksteal` | 与 load 同阶（初始按用例数均分再偷取），收益不超过 load | 同 load 的隔离问题，不优先 |
| `--dist loadscope` | 在本仓库与 loadfile **完全等价**（没有测试类，scope=模块） | 不用试 |
| conftest 哈希分片 + `--dist loadgroup` | 可做到 ~440s，且不挪测试代码 | nodeid 带 `@group` 后缀，影响 `-k`/报告可读性；需全部用例打组标记 |

**结论**：
- **纯测试侧、零产品风险路线**（不碰 `src/`）：保留 `loadfile` + 拆 2 个重文件 + 84 个慢用例模板化 + `docs` marker 分层，
  模拟 **757→492s（-35%）**；其中模板化单做 **-25%**、拆文件单做 **-29%**，两者互补（都换掉长尾文件）。
- **允许改产品时**：P0-1/2/3 对总成本的削减更大（原型实测 385s），且 `tm` 命令本身也变快；与拆文件叠加预计再降到 **~330–380s**。
- `--dist load` 干净实测 579s（-24%）、605 全过，但会放大「session 模板每 worker 重建」和 server 用例时序假设，
  **不建议作为唯一改动上线**，可作为后续优化；若用，给 server 用例加 `xdist_group` 兜底。

### 2.3 P1 细节

- **P1-1 读路径 `query_many`**：把 `find`（21）、`history`（18）、`export`（19）、`box show`（6）、`aliquot add`
  读阶段（12）等「一个用户动作 → N 条 SELECT」的代码改成一次批量读。实测原型 9 条查询 18→2 次子进程（7.8x）。
  预期全量再省 ~300–500 worker 秒，且这些正是 P0 之后剩下的读大头（docs/read_side/csv_io 文件）。
- **P1-2 flush+commit 合并**：目前每个写命令固定 1 次 flush + 1 次 commit。实测把
  `START TRANSACTION; DML; COMMIT; CALL DOLT_ADD; CALL DOLT_COMMIT` 放进同一条 `dolt sql -q`，
  1 次调用返回 commit hash，`dolt_log` 正确。全量写命令约数百次 → 再省数百次调用。
- **P1-3 open 固定成本**：`open()` = `read_meta` 2 次调用 + 无 server 时 1 次 `pgrep`（实测 26.4ms wall）。
  先探端口再 pgrep、`read_meta` 传 `columns=False`，每次 open 省 1 次调用 + 1 个子进程。
  套件里 ~170–190 次 CLI 进程内命令 + 15–40 次直接 open + 60–90 次显式 read_meta → 约 260–320 次调用（约 2%）。
- **P1-4 模板缓存类小项**：`template_workspaces` 每个 xdist worker 各建 base+full 两个模板（104 次调用/worker，
  基线里 3 个 worker 合计 ~118 worker 秒）；可跨 worker 共享缓存（文件锁）并懒构建变体；`dolt_repo`/`make_ledger`
  每个测试 3 次 dolt 调用（2 config + 1 init），可改 session 配置 + 空 repo 模板复制。
- **P1-5 去冗余查询**：uuid 的 `next_id` 现在做全表 `LIKE '%'` 扫描（产品在大账本上也是 O(n)）、
  `show_*` 重复 fetch 已解析的行、CSV 导入规划器对同一 ref 重复 resolve。

### 2.4 P2：收益最大但属于基础设施改造——测试走常驻 server（**已落地，实测 -81%**）

> 状态：已实现（工单 15–19，设计见 `docs/test-server-harness.md`）。实测：全量 **758.27s → 154.83s**
> （默认 loadfile，614 passed；`--dist load` 下 **141.50s**，-81.3%）；worker 时间 4,323s → ~894s；
> CPU 1,369s → 260s。`test_schema_diff.py` 单文件 757s → 49s，`test_validation` 500→41s，
> `test_objects` 491→89s，`test_docs_commands` 565→95s。剩余 embedded 覆盖用 `-m embedded` 跑（89 个测试）。

- 实测：`dolt sql-server` 冷启动 464ms、`ServerDriver` 连接 56ms、**单查询 0.40ms**（DictCursor 自带全列，无 csv 二次探测）。
- 若 dolt 用例改用 server：每次 init 的 34–67 条 DDL 从 2–5s 降到 ~30–50ms；find/history/export 等从秒级降到毫秒级；
  **墙钟目标 1–2 分钟**，之后瓶颈变成 Python/断言/schema 加载。
- 落地要点/风险：
  1. 不能「server 已打开数据目录再用 embedded `dolt init` 同一目录」；模板要先在 server 外建好，或通过 server `CREATE DATABASE`。
  2. 每 worker 一个 server（session 级）+ 每测试独立 database/ledger，或用固定模板库复制；
     `list_ledgers`、`server start/stop` 等 workspace 测试必须继续走 embedded/真实 server 生命周期。
  3. 必须保留 embedded 路径的覆盖（现有 `tests/test_driver_contract.py` 已参数化 embedded/server，
     docs quickstart 也天然覆盖 server），建议新增/保留一组 embedded 专项测试。

### 2.5 不建议做（有实测反例）

| 想法 | 反证 | 结论 |
|---|---|---|
| APFS clonefile（`cp -c`）替代 `shutil.copytree` | 17.1ms vs 25.8ms（模板 14 文件/170KB） | 不做 |
| `dolt backup`/`dolt clone` 替代目录复制 | 至少 1–2 次 dolt 调用（0.15–0.3s）+ 目录操作 | 不做 |
| CLI 测试的 subprocess 改进程内 | 19 处已经是 `CliRunner` 进程内，真实 subprocess 仅 1 处 | 无需做 |
| 清理 pytest tmp_path | 只保留最近 3 次 session，删除发生在下次启动，~1–2s | 收益可忽略 |
| 单纯加 `-n`（10→更多） | CPU 等效占用仅 1.8 核，I/O/进程启动是瓶颈，加并发只会让单次更慢 | 不建议，需先减调用 |
| 把 test_presets 改成模板 | 模板构建成本 ≥ 它自己的 3 次真实 init，且丢「每个 preset 独立 init」覆盖 | 不做 |
| 共享 ledger + `dolt reset --hard` 复用 | 实测 `CALL DOLT_RESET('--hard')` reset 到 HEAD 是 no-op（新表/行都还在），且需处理 ledger.yaml 回滚 | 不做 |
| embedded 驱动改常驻 `dolt sql` 进程 | 任何 SQL 错误都会杀死进程，需要重启+状态恢复（实测重启时间不稳定） | P2 备选，不优先 |

---

## 3. 已实测的组合验证（原型补丁）

为了验证上述 P0 项叠加后的真实效果，在一份 `/tmp` 仓库副本上打了 3 个补丁（不改本仓库源码）：

1. EmbeddedDriver 级 **DDL 批量化**（连续 DDL 合并成一次 `dolt sql`；覆盖 `init_ledger` 和 `schema apply` 两条路径）；
2. `init_ledger` 末尾 **跳过 refresh_meta**（P0-2）；
3. `read_meta` 用 **`columns=False` 单次探测**（P1-3 的一半）。

用干净环境（无计数 wrapper）+ 与基线相同的 10 worker，先测「只换派发」、再叠加补丁后跑全量：

| 运行 | 墙钟 | passed | CPU(user+sys) | 说明 |
|---|---:|---:|---:|---|
| A. 基线：原代码 + loadfile | **758.27s** | 605 | 1369s | §1 基线（PATH 上恰好有计数 wrapper，估计墙钟虚高 10–15s） |
| B. 原代码 + `--dist load`（干净） | **579.07s（-24%）** | **605** | 1333s | 只改 1 行配置；**3 个 server 失败未复现**（确认是 wrapper 假象） |
| C. 原型 3 补丁 + `--dist load`（干净） | **385.35s（-49% vs A）** | 605 | 799.7s | worker 时间 4322.8→2875.8s（-33%） |

> 归因：派发方式贡献 -24%，产品补丁在派发基础上再贡献 -34%（579→385s）。
> 注：B 运行期间另一名队友的微基准曾短时把 load 推到 52，B 的墙钟可能略偏高（保守按 579s 计）；
> C 是独占机器时测的，可信度高。

逐文件 worker 时间（秒，基线 → 原型）：

| 文件 | 基线 | 原型 | 变化 |
|---|---:|---:|---:|
| test_validation.py | 499.7 | 151.3 | **-348.4** |
| test_ledger_init.py | 427.4 | 93.3 | **-334.1** |
| test_docs_commands.py | 564.9 | 362.8 | -202.1 |
| test_remote_backup.py | 268.4 | 107.4 | -161.0 |
| test_read_side.py | 540.5 | 399.5 | -141.0 |
| test_schema_diff.py | 757.4 | 655.9 | -101.5（该原型未含 P0-3 内省批量） |
| test_presets.py | 143.7 | 43.3 | -100.4 |
| test_csv_io.py | 359.3 | 296.8 | -62.5 |
| test_cli_objects.py | 154.7 | 104.6 | -50.1 |
| 其余 | 57.9→97.6 | — | 分布/并发噪声，无回归 |

> 该原型没有做 P0-3（schema 内省批量）、P1-1（读路径批量）、P0-4（测试模板化），
> `test_schema_diff.py` 仍是最大剩余项（655.9s worker，其中 60% 是 plan 内省）。
> 加上 P0-3 预计该文件再降 **~200–270 worker 秒**（656→~390–450s），墙钟约再降 20–40s。

---

## 4. 测量口径与注意事项（避免踩我们踩过的坑）

1. **不要用「计数 wrapper」当 PATH 里的 `dolt`**：wrapper 会多一个 shell/`time` 进程；
   更关键的是 `Workspace.start()` 的 pid 文件会记录 wrapper 的 pid，`server stop` 的 SIGTERM 杀不到真正的
   `dolt sql-server`，导致 3 个 server 生命周期测试「假失败」。**干净环境复跑（579s / 385s 两次）均 605 全过**，
   证明这 3 个失败是测量污染，不是 `--dist load` 本身。计数请用「进程内 monkeypatch + 单文件」方式
   （`perf-notes/test-harness.md` 的 §1 方法 A），或在独立 session 里只统计不追求正确性。
2. **本沙箱环境屏蔽了 `ps`**（`Operation not permitted`），跑测试需要 PATH 前置一个只输出空的 `ps` shim
   （本次测量时放在临时目录 `.perf/shim/ps`）；这是环境限制，与测试性能无关。
3. 不同配置对比时保持 `PATH`/环境一致；`loadfile` 的墙钟取决于「最慢文件」，`load` 的墙钟取决于「最慢单测 + 总工作量」，
   不要只用一个指标下结论。
4. 所有微基准在空载下测一次、10-worker 全量下再测一次——并发会放大 3–5 倍，只报空载数字会低估问题。

---

## 5. 建议的实施顺序

1. **第一天（低风险、立竿见影）**
   - P0-1 + P0-2（产品 2 处小改，配 1 个「init 后 check/fingerprint/commit 正常」的回归断言）；
   - P0-5：拆 `test_schema_diff.py` / `test_docs_commands.py`，保留 `--dist loadfile`；这是**不依赖产品改动**的 -29%；
   - P0-4 只有在「暂时不做 P0-1」时才值得；若先做 P0-1，则只做 conftest 的 fast 工厂正确性前置修复。
   预期：**纯测试侧路线**（拆 2 文件 + 84 用例模板化 + docs marker）约 **8 分钟（-35%）**，零产品风险；
   允许改产品时（叠加 P0-1/2）约 **5–6 分钟**（实测原型 385s，仍缺 P0-3 与拆文件）。
2. **第一周**
   - P0-3（`query_many` + `_introspect` 批量化）——注意保留 driver contract 的类型/NULL 断言；
   - P1-1 读路径批量（先 `find`/`history`/`export` 三个最大头）；
   - P1-2 flush+commit 合并、P1-3 open 小项、P1-4 模板缓存类。
   预期：**~3–4 分钟**。
3. **后续（按团队对测试基础设施的投入意愿）**
   - P2-1 测试夹具改常驻 server（保留 embedded 专项覆盖），目标 **~1–2 分钟**；
   - 若还嫌慢，再按「纯 Python 用例 <2s」的分层策略优化日常开发回路。

---

## 附录 A：复现命令（本报告关键实验）

```bash
# 基线（与 pyproject 默认一致）
.venv/bin/python -m pytest -q --durations=0

# 干净环境下的逐用例动态派发（原代码，实测 579.07s / 605 passed；<ps-shim> = 只输出空的 ps shim 目录）
PATH="<ps-shim>:$PATH" /usr/bin/time -p .venv/bin/python -m pytest -q -n auto --dist load

# 原型（/tmp 副本；DDL 批量 + 跳过 refresh_meta + read_meta 单探）→ 385.35s / 605 passed
cd /tmp/tm-proto && PYTHONPATH=/tmp/tm-proto/src \
  PATH="<ps-shim>:$PATH" <原仓库>/.venv/bin/python -m pytest -q -n auto --dist load
```

## 附录 B：证据文件（本地临时产物，未提交）

| 文件 | 内容 |
|---|---|
| `perf-notes/prod-hotpath.md` | 产品代码热路径：调用模型、逐操作调用表、DDL/内省批量原型、server vs embedded 实测、风险清单 |
| `perf-notes/test-harness.md` | 测试夹具：逐夹具成本、per-preset DDL/调用数、模板复制实测、优化矩阵与反例 |
| `perf-notes/pytest-arch.md` | pytest/xdist：基线逐文件表、`--dist load` 实测、-n 实验、拆分模拟与建议 |
| `/tmp/baseline_full.log` | 基线完整 `--durations=0` 输出（758.27s / 605 passed） |
| `/tmp/proto_load_clean.log` | 原型全量（385.35s / 605 passed） |
| `/tmp/dolt_calls.log` | 完整 session 的逐次 dolt 调用记录（10,228 / 13,072 次两次 session，用于分类统计） |
| `/tmp/arch_load_clean.log` | 原代码 + `--dist load` 干净全量（579.07s / 605 passed） |
