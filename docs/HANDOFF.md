# 交接说明

给下一个接手的人。读这份就够，不需要翻聊天记录。

## 现在是什么状态

**已完成**：LangGraph / LangChain 的对照实现与三组实验；Dify 1.16.1 的 debugger HTTP 重试、
Human Input、API 重启和 worker 崩溃；以及独立 Published API `blocking` / `streaming` executor
attribution、默认 early-ACK worker 崩溃、experiment-only late-ACK whole-worker 对照，以及独立的
prefork feasibility + no-fault normal control。结论见根目录 `README.md`，设计理由见
`docs/DESIGN.md`，原始数据在 `evidence/`。

一句话版本：LangGraph 的检查点给的是"能接着走"不是"不会重复做"；归档的崩溃注入 30 次 × 20 步中，
纯检查点默认配置记录 128 条重复副作用、最强持久化 `sync` 下 20 条、叠加自建两段式意图账本后 0 条。
其中 async 的具体重复条数受 checkpoint 调度时序影响，128 是该归档样本，不是跨运行不变量。

Dify 一句话版本：debugger 不重试时 HTTP 500 被当作普通输出，配置 3 次重试会把已落盘副作用
做 4 次；Published workflow 的 `blocking` 在 API 进程内执行，`streaming` 才进入 Celery。有效
streaming fault 中，exact active task 已 `acknowledged=true` 后 worker 以 137 退出；约 3 分钟内
effect 仍为 1 次，但 run / HTTP node 停在 `running`，Redis 没有 queue 或 unacked delivery 可供
visibility timeout 恢复。只给 exact workflow task 开 late ACK 的对照则保住了 Redis unacked delivery；
后续冷启动时 same task id 以 `redelivered=true` 重投，run 最终成功但 effect 变成 2 次，判定
`duplicate`。后续 prefork 无故障切片又证明了真实 OS pool child 与 exact task / Redis delivery 的绑定；
orchestrator 创建 release 后内部 HTTP 200、run 成功、effect=1、unacked 清零，但没有注入故障。环境、run id 与
脱敏快照见 `docs/DIFY-STATUS.md` 以及八个 `evidence/dify-*.json`。

证据边界：旧 debugger 的 HITL 重启前暂停状态和 worker 精确 kill 时刻没有独立截图/时间戳；
Published fault 则保留了 marker、exact active task、精确 kill / restart 时间、exit 137、Redis 三阶段
快照和约 3 分钟终态快照。late-ACK fault 又保存了原 delivery、cold-start same-task redelivery、
第二次 effect 与最终 node rows；但中间有外部 turn / WSL 暂停，不能声称恰好在 120 秒重投。
prefork normal control 另存两次稳定 parent/child 拓扑、blocked exact task / PID / delivery、内部 HTTP 200、
最终清理和四服务 rollback；第一次 504 control 与一次请求前工具失败均保留为无效样本。各切片都是
本地单次有界观察，不得互相补证或泛化。

## 环境与复现

```bash
uv sync --locked --group dev
uv run python -m guarded_loop.crash_bench --runs 30 --steps 20 --out _bench
uv run python -m guarded_loop.eval_trace
uv run python -m guarded_loop.llm_run          # 需要 OPENAI_API_KEY
```

环境同步完成后，crash benchmark 与 eval 不需要网络或 API key；`llm_run` 会真的调 `gpt-4o-mini`。
没有 `uv` 时可用 `pip install --require-hashes -r requirements-lock.txt` 安装运行依赖。

开发时用的是 Python 3.13.7 / Windows。`uv.lock` 冻结完整跨平台解析，`requirements-lock.txt` 与
`requirements-dev-lock.txt` 是带 artifact hash 的 runtime / dev export。

一次 host terminal observation 在 post-rollback capture 结束（`2026-08-30T04:33:39.867034Z`）之后、
脱敏 raw 快照记录（`2026-08-30T04:38:38.0855202Z`）之前看到 Dify 两个 WSL 发行版与 sink
均为 stopped，数据仍保留；精确 observation 时刻没有归档，这也不是无时限的当前状态承诺。
最新容器快照本身是在隔离服务运行时采集。Published 栈位于独立发行版
`DifyBench-Isolated-20260828`、Compose project `dify_pub_20260828`；恢复它之前先检查 Docker
`DOCKER-FORWARD` 是否含新 bridge，并从同网容器验证 Redis `PONG`。原 `Ubuntu` 中的
`crash-worker-001` 和隔离 DB 中的 `pub-stream-worker-crash-001` 都是有意保留的悬挂证据，不要重放、
手工改终态或混入下一次 key。prefork overlay 清理与 base-only Compose 重建已执行；四服务的归档均显示
mount count 0、运行目标 hash 恢复，API / worker 的 effective task 设置为 false/false、transport options
为空、live Redis channel 为 3600 秒，worker 回到 gevent / max 4 / prefetch 4 / 无 OS child。旧 late-ACK
artifact 自身仍只有 worker rollback 证据，不能被新切片反向升级。不要假设重启后仍是实验配置，也不要
复用历史 child PID 148。

## 代码结构

```
worker.py        一次运行 = 一个子进程（崩溃注入靠杀进程，不靠抛异常）
  └─ graph.py    LangGraph StateGraph：plan → gate → act → route
       └─ tools.py   固定注册表 + pydantic 双向校验 + 意图账本（不 import 任何 langgraph 符号）
crash_bench.py   三组对照的崩溃注入基准 + timeout / trial invariant / 环境元数据
eval_trace.py    15 个确定性 case + 判据/实现 SHA-256 manifest
llm_run.py       真实模型链路 + 默认拒绝的统一审批边界
dify_sink.py     Dify HTTP sink：fsync、atomic marker、请求/并发上限、非 loopback unsafe gate
dify/            三个可导入的 Dify DSL
evidence/dify-published-crash-*.json   Published API executor / worker-crash 证据
evidence/dify-published-late-ack-*.json   Published API late-ACK redelivery / duplicate 证据
evidence/dify-prefork-control-*.json   Published API prefork feasibility / no-fault control 证据
```

`tools.py` 不依赖 LangGraph 是有意的——只有契约层能独立存在，
"哪些保证是框架给的、哪些是自己给的"才问得出来。改代码时请保持这条边界。

## 已完成的维护 detour（2026-09-01）

**状态：离线维护已闭合；没有执行任何 Dify live fault。** 本切片修复了审批、三态账本、canonical
幂等参数、图层未知工具和真实模型外循环的 fail-closed 语义；benchmark/eval 增加 timeout、invalid
trial、实现 hash 与受控临时目录；Dify sink 增加请求/并发上限、atomic marker 和非 loopback unsafe
gate；同时补齐 22 个 pytest、strict mypy/Ruff、`uv.lock`、hash requirements export 与分层 CI。

验证结果：`ruff check` 通过；strict mypy 对 8 个 source file 为 0 issue；pytest 为 22/22；
deterministic eval 为 15/15，manifest `8bfab4971eb9ec62...`；Python 3.11 isolated smoke 为
15 passed / 7 deselected；hash requirements 的 `pip --dry-run --require-hashes` 通过。最终 30 x 20
crash benchmark 的 90 个 trial 全部有效：async checkpoint-only 为 29/30 runs、130 duplicates；sync
为 20/30、20 duplicates；ledger 为 0 duplicate，20 次 `UNCERTAIN_HALT` / 10 次正常。该次 async
130 与归档样本 128 的差异再次说明具体条数 timing-sensitive；没有替换 `evidence/` 中的原始归档。

维护完成后仍从下一节逐字恢复，不新增其他下一动作。`DifyBench-Isolated-20260828` 未被启动、恢复、
kill 或改写。

## 精确故障实验恢复点（detour 完成后的唯一下一动作）

只在 `DifyBench-Isolated-20260828` 中另开一个 **exact prefork child-loss fault** 切片。重启后先重新验证
bridge / Redis PONG / API health / worker ready，再以新 key 重建并归档与本次相同的 exact-task late ACK、
900 秒 timeout、prefork / concurrency 1 / prefetch 1，以及稳定 parent / 唯一 child 拓扑。历史 PID 148
已经随回滚消失，绝不能作为新 fault 目标。

新请求必须先同时满足：attempt 1 已 `fsync`；exact run 与 Celery task active；active
`worker_pid` 等于当次唯一 pool process 和 direct OS child 的 container PID；Redis 只有同一 task
的一条 unacked delivery。fault target 必须归档为 `(worker_container_id, child_container_pid,
child_host_pid, child_host_start_ticks)`；kill 前立即重采四元组，并确认 parent 身份、container ID
和 restart count 均未变。从 host namespace 执行时只杀 `child_host_pid`，只有在同一 container PID
namespace 内执行时才使用 `child_container_pid`；不杀 controller 或整个 container。

child kill 后继续保持 release 不存在，并连续归档 parent / container 连续性、旧与 replacement
child 身份、active task、Redis delivery / redelivery、worker logs、run / node 状态和 effect count。
只有同一 task 已在 replacement child 上 active，且 Redis redelivery 与 sink attempt 2 都已归档，
才创建 release 并收集终态；否则必须连续观察到 kill 前记录的 `visibility_due_epoch + 120s`
仍无重投，只能判 `bounded_no_redelivery / uncertain`，此后 release 仅可用于清理。不要预设结果
来自 parent 的即时 reject，还是后续 visibility restoration。任一 kill 前归因门槛失败就判该尝试无效，
不补做 kill；结束后再次完整回滚并保存四服务 baseline 证据。

### 判断标准

不管补哪个框架，标准是同一条：**能不能说出一个该框架文档里写着、但用的人多半没意识到的语义。**
说得出来才算用过，说不出来就还是跑了个教程。

本仓库里那条是 `durability`——三档配置控制的是已完成步骤何时落盘，
管不到节点执行到一半崩掉，所以副作用幂等属于调用方责任。

Dify 新增的那条是 executor + ACK boundary：Published `response_mode` 先决定 API thread 还是
Celery worker；Celery task 若在执行前 early ACK，worker 崩溃后 Redis visibility timeout 没有
未确认 delivery 可以恢复；只改成 late ACK 会保住 broker recovery path，但重投整个 workflow task
仍可能重复已经落盘的非幂等副作用。

## 结论的适用边界

写任何基于本仓库的材料前请一起说明：

- **这是为对齐语义写的对照实现，未上线、无真实用户。**
- 不要把结论说成"LangGraph 不行"。那是它文档写明的语义，说成缺陷是误读。
- 要一起说框架更强的那一块：`interrupt` + 检查点能让进程退出后再恢复审批，
  比自研那套同步阻塞干净。
- 只覆盖 StateGraph / checkpointer / interrupt 三块；
  LangChain 生态里的 RAG、向量库、Agent 预制件未使用过；Dify 只覆盖 1.16.1 的 HTTP Request、
  Human Input、本地 debugger 草稿和单 worker Published API `blocking` / `streaming`、early ACK、一次
  experiment-only late ACK whole-container 对照与一次 prefork 无故障 control；pool-child-only reject 隔离、
  连续 timeout latency、集群、定时任务、生产流量与 Coze 等其他平台仍是空白。
- 本仓库的归档数字（30×20、async 样本 128 / sync 20 / ledger 0、15 个 case）与 Guarded Desktop Agent 的数字
  （30×100、1420 项测试、13 个 case）是两套独立实验，不要混用。
