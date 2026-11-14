# BOM 叶件用量数仓

制造企业成本数仓的多级 BOM 叶件用量表：浏览器维护父子关系与用量，整批提交后
沿路径乘用量、多路径相加，把结果**落库**；查看与下载只读已落库的快照。

## 快速启动

```bash
docker compose up --build
```

- 网页 A：<http://localhost:8000>（进程 `web-a`）
- 网页 B：<http://localhost:8001>（进程 `web-b`，与 A 共用同一个 SQLite 文件）
- 用量溯源页：<http://localhost:8000/trace>（也可从用量表每行的「溯源」进入）
- `seed` 一次性任务会通过 HTTP 载入演示结构，并把叶件 `L-4` 提升为装配件
  （`L-4 → L-6/L-7`），两个样例都可直接看到。

用量溯源页的用法：从已发布用量行选择「装配件（顶层件）」和「叶件」，指定查看
贡献最大的 1～10 条路径。页面显示每条路径的完整料号链、沿途每条关系的用量与
路径用量（沿途用量之积），以及其余路径的贡献合计。所有数值用精确整数相加：

```
落库用量 = 已列出路径用量之和 + 其余路径贡献合计
```

同样的查询也可直接调用 `GET /api/usage-trace?top=FG-1&leaf=L-3&limit=5`。

溯源只使用**所读版本**的父子关系：边和落库用量行在同一个读事务里取出，因此
另一个 Web 进程刚提交的新 BOM 不会混进本次结果。若按该版本父子关系复算出的
总量与落库值不同（例如数据被库外修改），页面会明确提示「数据不一致」，列出
的路径仍按复算值计算，「其余路径贡献合计」也只取复算总量减去已列合计，
差额不会被算成任何其他路径。

可选：在两个真实容器之间验证交错修改：

```bash
docker compose --profile demo run --rm interleave
```

本地（不使用 Docker）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000            # 第二个进程换端口即可
python scripts/concurrency_demo.py           # 起两个真实进程做交错验证
pytest -q                                    # 73 项测试
```

## 功能与约束

| 要求 | 实现 |
| --- | --- |
| 最多 100 个料号、200 条父子关系 | `validate_limits`（超限返回 409，整批不写） |
| 浏览器成批增改删、正整数用量 | 单条操作先进入「批改清单」，「提交整批」一次提交；用量 1..1e9 |
| 每个装配件展开到叶件、沿路径乘用量、多路径相加 | `BomStore.expand`（按子装配记忆化，共享子装配不会指数爆炸） |
| 结果必须落库 | `leaf_usage` 表中按版本物化，启动/查看/下载都不再展开 |
| 以整批修改后的结构判环，显示路径，遇环整批不改 | `find_cycles` 基于最终结构；409 返回 `paths` 如 `A → B → C → A`，事务回滚 |
| 两个 Web 进程共库 | 同一 `BOM_DB_PATH`，WAL + `busy_timeout`；`docker-compose.yml` 起两份 |
| 递归展开不占写锁，其他修改可先提交 | 展开与判环都在内存快照上完成，之后才 `BEGIN IMMEDIATE` |
| 迟到的展开不能覆盖新结构/漏掉新依赖 | 提交时对 `bom_head.version_seq` 做 CAS；失败则按最新结构**重新规划并重新判环** |
| 分别无环的并发修改不能合成环 | 复核在最新结构上重跑，合成环会被拒绝 |
| 关系与受影响用量表同时更新，失败保留旧版 | 新版本写入 `bom_edge` + `leaf_usage` + 切换 `bom_head` 全在同一事务 |
| 查看/下载不混两个提交、不临时展开 | 每次请求在其读事务内解析一次 `bom_head`，只 SELECT 物化行 |
| 用量溯源：选已发布用量行、看贡献最大的 1～10 条路径 | `/trace` 与 `/api/usage-trace`；同一读事务内取边与落库行 |
| 共享子装配路径极多，查询不枚举所有路径 | `trace_top_paths` 只保留每个节点最优的 `limit` 条后缀，`O(节点 × 出度 × limit)` |
| 路径用量乘积 + 其余路径合计精确等于落库用量 | 全程 Python 整数；`other = 复算总量 - 已列合计`，不枚其余路径 |
| 贡献相同按完整料号路径排序 | `TracePath` 按 `(-qty, path)` 排序 |
| 另一进程改 BOM 时只显示所读版本 | 边与用量行在同一 `BEGIN` 读事务内解析一次版本 |
| 复算值与落库值不符时提示，不把差额算成其他路径 | `consistent=false` 仍按复算值给出 `other`，页面明确标示差额未归入任何路径 |
| 重启保留结果 | 数据在卷 `bom-data` 的 SQLite 文件中；版本与用量行都在 |
| 共享子装配 / 叶件转装配件 / 可暂停计算的交错修改样例 | 见下文 |

## 并发模型

```
读快照 ──► 应用批改 ──► 判环 ──► 展开（全部在内存，无写锁）
                                        │
                                        ▼
                        BEGIN IMMEDIATE ──► CAS 版本号
                           成功：写新版本 + 用量 + 切 head + 清理旧行 → COMMIT
                           失败：ROLLBACK，回到第一步按最新结构重来
```

- 写锁只在最后一段纯 SQL 事务内持有，毫秒级；期间别的进程可以正常提交。
- CAS 失败说明期间有新提交，此时**旧计划作废**：重新读取结构、重新应用批改、
  重新判环、重新展开。因此迟到的展开不会覆盖新结构，也不会漏掉新依赖。
- 两次分别无环的修改若合起来成环，后提交的一方会在重规划后的结构上发现环而
  整批失败（返回路径）。测试 `test_two_acyclic_edits_cannot_combine_into_cycle`
  与 `scripts/concurrency_demo.py` 场景 2 覆盖此点。
- 只保留当前发布版本的边与用量行（同事务内清理），因此重启后读到的是同一份
  结果，且存储量有界。

## 样例

1. **共享子装配**：`FG-1 → SA-1(2) → L-3(5)` 与 `FG-2 → SA-1(3) → L-3(5)`。
   用量表里 `(FG-1,L-3)=10`、`(FG-2,L-3)=15`，同时 `(SA-1,L-3)=5`
   作为子装配自身的展开行。
2. **叶件转装配件**：`POST /api/demo/promote-leaf`（UI 上「叶件转装配件」按钮）
   在一批中把 `L-4` 改为由 `L-6(3)`、`L-7(1)` 组成；`FG-1` 的 `L-4` 行消失，
   `L-6/L-7` 行出现，全部来自同一次提交。
3. **可暂停计算的交错修改**：演示模式下 `X-Demo-Pause` 头会让进程在展开阶段暂停
   （写锁尚未获取）。此时另一个进程可以提交；暂停方醒来后因 CAS 失败而重规划。
   可用 UI 的「写 A 侧 / 写 B 侧」按钮、`/api/demo/interleave/{a|b}`，或
   `scripts/concurrency_demo.py` 复现。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前版本的边、用量、版本号、上限 |
| POST | `/api/batch` | `{"ops":[...]}` 顺序执行；`replace` 为整体替换；`{"reset":true,"upserts":[...]}` 直接重建 |
| GET | `/api/leaf-usage?top=&leaf=` | 已落库用量（单快照） |
| GET | `/api/usage-trace?top=&leaf=&limit=` | 某条已落库用量行的贡献路径（`limit` 1～10） |
| GET | `/api/leaf-usage.csv?top=&leaf=` | 下载 CSV，带 `X-Bom-Version` |
| GET | `/api/usage-summary` | 只读汇总 |
| POST | `/api/demo/seed` / `/api/demo/promote-leaf` / `/api/demo/interleave/{a,b}` | 演示样例 |
| GET | `/api/demo/scenarios` | 样例说明 |

批改示例：

```json
{"ops": [
  {"op": "upsert", "parent": "FG-1", "child": "SA-9", "qty": 2},
  {"op": "delete", "parent": "FG-1", "child": "L-1"},
  {"op": "upsert", "parent": "SA-9", "child": "L-8", "qty": 3}
]}
```

错误：422 校验失败（非正整数、非法料号、超限）；409 环路（含 `paths`）或容量超限。

## 文件

```
app/store.py        SQLite 版本化存储、展开、判环、路径溯源、并发提交
app/main.py         FastAPI 应用工厂、API 与页面
app/demo.py         内置样例数据与交错修改用例
app/templates/      单页 UI（维护页 + 用量溯源页）
app/static/         前端脚本与样式
scripts/            两进程演示、HTTP 交错验证、种子任务、worker
tests/              pytest（73 项：展开、判环、原子性、并发、HTTP、溯源、UI）
docker-compose.yml  双 Web + 种子任务（+ 可选 interleave）
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

覆盖：多路径求和、共享子装配、叶件转装配件、深 DAG 记忆化、整批判环与路径、
`replace`/`reset`/顺序语义、容量上限、展开失败与写入中途失败的回滚、跨实例持久化、
展开期间无写锁、迟到展开重规划、并发合成环、读写快照一致性、CSV 单快照、
重启保留、两 Web 应用共库、用量溯源（精确求和、前 K 条路径剪枝与穷举对拍、
并列排序、单版本一致性、落库值不符提示），以及模板/JS 元素引用一致性。
