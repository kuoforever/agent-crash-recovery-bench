# LangGraph 受控执行回路

**一个问题**：把一段带副作用的受控执行回路交给 LangGraph 去跑，崩溃恢复时副作用会不会被重复执行？

这个仓库用崩溃注入去量它。做法是用 LangGraph 的 StateGraph、SQLite 检查点与 `interrupt`
重建一段原本自研的受控执行回路（对照对象是我自己的
[Guarded Desktop Agent](https://github.com/kuoforever/guarded-desktop-agent)），
然后把两边能给的保证逐条对齐，量出差在哪。

结论提前说：**LangGraph 的检查点给的是"能接着走"，不是"不会重复做"**——这是它文档写明的语义，
不是缺陷；但代价有多大值得实测一遍。

环境：Python 3.13.7 / langgraph 1.2.10 / langgraph-checkpoint-sqlite 3.1.1 / langchain-core 1.5.3 / langchain-openai 1.4.1

## 文档

| 文件 | 内容 |
|---|---|
| 本文件 | 结论与数字 |
| `docs/DESIGN.md` | 为什么是这个形状：三态语义、两段式账本、崩溃注入方法学、已知限制 |
| `docs/HANDOFF.md` | 交接：做完了什么、还差什么、红线在哪 |
| `docs/career/` | 按 JD 选取的项目证据，以及教学式协作协议；不替代本页、设计或交接 |
| `docs/DIFY-STATUS.md` | Dify 1.16.1 对照实验、复现环境与运行状态 |
| `dify/` | 三个可导入的 Dify DSL：不重试、重试 3 次、人工审批 |
| `evidence/` | 原始数据：崩溃注入报告、真实模型 trace，以及 Dify debugger / Published API early-ACK、late-ACK、prefork normal-control 与 exact child-loss 摘要、脱敏快照和 SHA-256 manifest |

## 跑什么

```bash
uv sync --locked --group dev
uv run python -m guarded_loop.crash_bench --runs 30 --steps 20 --out _bench
uv run python -m guarded_loop.eval_trace                                # 15 个确定性 case
uv run python -m guarded_loop.llm_run                                   # 真实模型链路（需 OPENAI_API_KEY）
```

环境同步完成后，crash benchmark 与 eval 不需要网络或 API key。
没有 `uv` 时可用 `pip install --require-hashes -r requirements-lock.txt` 安装运行依赖；
`uv.lock` 与两个 requirements export 都冻结了完整依赖和 artifact hash。

PR 的 offline CI 先在 Linux / Python 3.11-3.13 跑格式、lint、strict mypy 与无子进程 smoke，
再在 Windows / Python 3.13 跑全部 pytest、冻结 eval 和 30×20 crash benchmark。网络模型和 Dify
故障实验不属于这个自动门禁。

## 结论一：LangGraph 的检查点给的是"能接着走"，不是"不会重复做"

崩溃注入方式与我自己项目里那套一致：子进程跑到指定步骤 `os._exit(70)` 硬退出（不跑 finally，真崩溃不给清理机会），
父进程用同一个 `thread_id` 恢复，最后数落地的副作用有没有重复。30 次运行 × 每次 20 步，崩溃 30/30 全部确认。

| 对照组 | 有重复副作用的运行 | 重复副作用条数 | 停止码 |
|---|---|---|---|
| 纯检查点 · `durability="async"`（默认，归档样本） | 29 / 30 | **128** | 全部静默跑完 |
| 纯检查点 · `durability="sync"`（最强持久化） | 20 / 30 | **20** | 全部静默跑完 |
| 检查点 + 自建意图账本 | **0 / 30** | **0** | 20 次 `UNCERTAIN_HALT` / 10 次正常 |

数字能逐条对上账，这点比数字本身重要：

- `sync` 下每次崩溃只重放**正在执行的那个节点**，所以恰好 1 条重复 × 20 次运行；
  `async` 下已完成但未落盘的 superstep 也会一起丢。表中的 128 是归档样本值，具体条数受异步
  checkpoint 调度时序影响，不是跨运行不变量；稳定结论是它会重放尚未持久化的已完成 superstep。
- 20 次 = 副作用已经落地的两个注入相位（`post_apply` + `post_commit`）各 10 次。
  `pre_apply` 那 10 次崩在副作用发生之前，重放一次反而是对的，所以不计重复。

**这不是 LangGraph 的 bug，是它写明的语义**：`durability` 三档（`sync` / `async` / `exit`）
控制的是"完成的步骤什么时候落盘"，管不到"节点执行到一半崩了"——节点没返回就没有提交点，
恢复时必然从节点开头重跑。所以副作用的幂等性是调用方的责任。
我量的是这个语义在**带副作用的回路**里代价有多大：最好的持久化配置下也不是零，是每次崩溃恰好重复一次。

## 结论二：补到零重复要自己加账本，代价是会误停

自建的意图账本走两段式：`记意图(pending)` → `执行副作用` → `标记完成(done)`。
恢复后读到 `pending` 说明崩在中间，**这时唯一正确的动作是停机报不确定，不是重试**——
副作用到底发没发生，此刻没有任何信息能判定。

代价写清楚：20 次 `UNCERTAIN_HALT` 里有 **10 次是误停**（崩在 `pre_apply`，副作用其实根本没发生，
但账本里已经有 pending，分不出来）。这个方向选得对（宁可停也不重复做），但精度是有成本的，
要消掉这 10 次得让账本写入和副作用落到同一个事务里，文件型 sink 做不到。

## 结论三：审批闸门这块 LangGraph 比我自己写的干净

`interrupt()` + 检查点让图能在高风险动作**之前**挂起，状态落盘，进程可以直接退出，
之后用 `Command(resume=...)` 接着走。我自己那套是同步阻塞等审批，进程不能退。
这一条是框架确实做得更好的地方，不用嘴硬。

## 结论四：判据用 trace，不用自然语言

15 个确定性 case，判的是**调用序列 + 停止码 + 副作用条数**，不判模型说了什么。
判据与受保护实现一起用 SHA-256 冻进 `eval_manifest.json`；缺失 manifest 不会自动建基线，
改判据或实现后必须显式复核并运行 `--update-manifest`。当前 15/15 通过。

覆盖：注册表外工具在契约层和图层都 fail closed / 参数与额外字段不合契约不进执行层 /
确定无副作用失败清除 pending / 副作用后无有效回执判 `uncertain` 且不重放 / 正常回路与重放 /
高风险动作挂起 / 严格布尔审批（字符串 `"false"` 不算批准）/ 不确定停机后不推进游标。

## 真实模型链路

`ChatOpenAI(gpt-4o-mini) + bind_tools`，固定注册表原样暴露给模型，
但**执行不交给 LangChain 的 ToolNode**——模型给的参数一样要逐字段过 pydantic schema 才准进执行层。
实跑序列：`read_status(t0)` → `write_note(t0, "hello")` → 收尾，两次调用都 `ok`。
模型若请求 `needs_approval` 工具，默认拒绝并停止；只有命令行显式传入
`--approve-tool submit_form` 才会允许该次运行执行它。

崩溃基准刻意不接模型：恢复语义要单独量，模型的随机性混进去就成噪声了。

## Dify 1.16.1 对照：执行模式先决定谁是 executor

本地 Docker Compose 版 Dify 1.16.1 上先跑了 debugger 草稿，再在独立 WSL / Compose 数据栈中
发布同一条 HTTP 工作流。HTTP sink 会先 `fsync` 副作用记录，再返回指定状态码；也可以在落盘后、
返回前阻塞，给硬杀进程留出确定窗口。

debugger 结论在 `evidence/dify-semantics-report.json`，原始快照在
`evidence/dify-raw-snapshot.json`；Published API 结论在
`evidence/dify-published-crash-report.json`，原始快照在
`evidence/dify-published-crash-raw.json`；late-ACK 对照在
`evidence/dify-published-late-ack-report.json` 与
`evidence/dify-published-late-ack-raw.json`；prefork 无故障对照在
`evidence/dify-prefork-control-report.json` 与
`evidence/dify-prefork-control-raw.json`；exact prefork child-loss 在
`evidence/dify-prefork-child-loss-report.json`、`evidence/dify-prefork-child-loss-raw.json` 与
`evidence/dify-prefork-child-loss-manifest.json`。可导入 DSL 在 `dify/`。实验 key 限定为
`[A-Za-z0-9_.-]{1,120}`，不把任意用户文本直接当作 JSON 测试输入。
child-loss manifest 的 34 项中只有 sink implementation 被 Git 跟踪，其余 33 项是实验宿主保留的
gitignored 原件；public clone 能检查脱敏转录和 manifest，不能独立重哈希或复现实验原件。

### Debugger 草稿：重试会重做整个 HTTP 节点

| 场景 | Dify 最终状态 | 副作用次数 | 实测语义 |
|---|---:|---:|---|
| HTTP 500，不重试、无错误策略 | `succeeded` | 1 | 500 作为普通输出向后传，工作流仍成功 |
| HTTP 500，最多重试 3 次 | `failed` | **4** | 初次调用 + 3 次整节点重试，每次都已产生副作用 |
| 人工审批前重启 API 容器 | `stopped` | 1 | 审批表单与恢复状态持久化；本次原调试器响应流未续接 |
| 副作用落盘后硬杀 worker | 快照时仍 `running` | 1 | 约 14 分 24 秒内未观察到重投或收敛；HTTP 节点仍为 `running` |

证据完整度也单独记账：HITL 在 API 重启前的 `paused / total_steps=2 / sink=0` 是当时操作者观察，
没有保存中间截图或查询 transcript；仓库可独立复核的是“表单创建 → API 新进程启动 → 表单提交 →
副作用落盘 → 最终运行停止”这条时间线。worker 的精确 `docker kill` 时刻同样没有留存，原始快照只给出
副作用落盘与 worker 重新连接之间的时间边界、退出码 137 和终端状态行。

### Published API：`blocking` 与 `streaming` 走不同 executor

固定 checkout `5456d4d…` 只作控制流语义参照；实跑身份由 Dify 1.16.1 image ID、已归档的 selected
entrypoint / config target 容器内 hash，以及 experiment overlay target hash 锚定。controller / service /
task 全树没有 byte binding，不能把 checkout commit 当成镜像构建证明。source control flow 与 worker log、active inspect
的运行时结果独立相符：Published workflow 的 `blocking` 分支在 API 进程内启动 Python thread；
`streaming` 分支才把 `workflow_based_app_execution_task` 投递到 Celery worker。因此“杀了 worker”
之前必须先证明目标 run 确实在那个 worker 上 active。

| 场景 | 最终/快照状态 | 副作用次数 | 实测语义 |
|---|---:|---:|---|
| `blocking`；effect 后杀 Celery worker | `succeeded` / 3 steps | 1 | 没命中 executor；外层 API 200，HTTP 节点把 Squid 504 当输出 |
| `streaming` 正常控制 | `succeeded` / 3 steps | 1 | exact run id 出现在 Celery task，证明 streaming 执行边界 |
| `streaming`；effect 后杀 active worker | 约 3 分钟快照仍 `running` / 0 steps | 1 | task 已确认；无 queue/unacked、无重投、无应用层收敛 |
| late-ACK `streaming` 正常控制 | `succeeded` / 3 steps | 1 | 阻塞时 task 未确认且 Redis `unacked=1`；释放后正常确认 |
| late-ACK `streaming`；effect 后杀整个 active worker 容器 | 后续冷启动重投并 `succeeded` / 3 steps | **2** | 同一 task id 以 `redelivered=true` 重投；恢复工作流但重复副作用 |
| prefork + late-ACK `streaming` 无故障控制 | `succeeded` / 3 steps | 1 | exact task 的 PID 同时命中唯一 OS child、pool process 与单条 Redis unacked；HTTP 200 释放后清零 |
| prefork + late-ACK；effect 后只杀 exact pool child | 同一 run `succeeded` / 3 steps | **2** | parent/container 保持；replacement child 上 same task/tag 立即 `redelivered=true`；恢复工作流但重复副作用 |

正式故障样本 `28e83e19-b8af-4344-8421-0b887b27712a` 在 kill 前同时满足三项归因：sink
已有 `attempt=1`；worker log 含 exact run id；`celery inspect active` 显示该 task 在目标 worker
上且 `acknowledged=true`。目标容器随后以 137 退出并显式重启。运行时复核得到 effective
`task_acks_late=false`、`task_reject_on_worker_lost=false`；kill 前、kill 后和重启后的 Redis
queue、`unacked`、`unacked_index` 都为空。

所以这次不该“继续等 visibility timeout”：Redis visibility timeout 处理的是未确认 delivery，
而这条消息在 worker 崩溃前已经确认。约 3 分钟的有界观察内没有第二次 effect，代价是数据库 run
和 HTTP 节点仍停在 `running`，SSE 客户端没有收到终态。它说明的是这一个配置下没有 broker
恢复入口，不是 Dify 所有部署永远不会恢复。

2026-08-30 的 late-ACK 对照只给这一个 workflow task 加了 effective `acks_late=true` 与
`reject_on_worker_lost=true`，并把 Celery Redis 的 broker、result backend 与全局
`visibility_timeout` 都设成 120 秒；API / worker 进程与 worker remote-conf 读数相符，live Redis
channel 也实测为 120 秒。
正常控制在 HTTP effect 阻塞时表现为 `acknowledged=false`、queue 0、`unacked=1`，释放后只执行一次并
清空 unacked，说明配置确实打到了 Published `streaming` 路径。

故障组在 attempt 1 已 `fsync` 后杀掉整个 active worker 容器，退出码 137；未确认 delivery 在 kill
前后和首次重启后都保留在 Redis。过期后的一段连续观察中它仍未重投，随后实验宿主被外部暂停，
所以不能声称“恰好 120 秒恢复”。后来冷启动 worker 时，同一 Celery task id 以
`redelivered=true` 被 broker 重投，sink 记录 attempt 2；释放后数据库 run 最终 `succeeded`，但原
effect node row 仍是 `running`，重投生成的 effect row 才成功。判定因此是 **duplicate**，不是
exactly-once recovery。原 SSE 客户端也没有收到 terminal event。

early ACK 与 late ACK 的差异由此能直接对齐：early ACK 样本在 worker loss 前已没有 broker delivery，
留下 1 次 effect 与悬挂 run；late ACK 样本保住了 broker recovery path，却把非幂等 effect 做了 2 次。
**可恢复性与 exactly-once 是两个独立问题。** 整个容器被杀时 Celery parent 也消失，因此这轮能归因于
Redis broker restoration，不能单独证明 `reject_on_worker_lost` 的即时 requeue 语义。

### Prefork：先证明真实 pool child，再只杀 exact child

随后单独开的 experiment-only 切片把同一个 task annotation 保留下来，将 visibility timeout 拉长到
900 秒，并把 worker 显式设成 `prefork` / concurrency 1 / prefetch 1。两次请求前快照都看到同一
worker、controller PID 1、唯一 direct OS child PID 148；`celery inspect stats` 同时给出
`celery.concurrency.prefork:TaskPool`、max concurrency 1、processes `[148]` 和 prefetch 1。
启动日志仍出现 gRPC / psycopg2 的 gevent-related patch，所以这不是“纯净 prefork”兼容性证明。
artifact 跨两次 WSL boot：最初 baseline / control-001 / 旧 rollback 属于 `8b2dd1ae…`，control-002 /
control-003 / 当次 rollback 属于 `d54236db…`。process identity 连续性只在 control-003 的
blocked → final 同 boot 内成立；rollback 结论是当次 boot 的 base / gevent 实测与旧 baseline
配置/拓扑等价，不是跨 boot 进程身份连续。

有效 control `pub-prefork-control-003` 阻塞时，同一个 Celery task 的 `worker_pid=148` 同时命中
pool process 与 direct OS child，active task 为 `acknowledged=false / redelivered=false`，Redis 是
queue 0、`unacked=1`、index 1，sink 已 `fsync` attempt 1。orchestrator 创建 release 后，HTTP 节点的**内部**
响应是 200，JSON 明确为 `attempt=1 / released=true`；同一 run 以 3 steps 成功，effect 仍为 1，
Redis 全清，worker log 只有 received / succeeded 各 1 次、没有 redelivery。第一次 control 的决定性
失败证据是内部 Squid 504，而不是 sink origin `200 + released=true`；release 内容时间、NTFS stat
与 client wall / monotonic 读数不能可靠建立跨时钟先后，不作因果 gate。第二次在发请求前暴露
client-name guard 缺陷；orchestration artifact 只证明 workflow request 未发出，没有归档失败后
DB / Redis / sink 快照，也没有单独归档三路径测试 transcript。当前 hash-pinned helper 已改用
exit code 判断，后续 control-003 在该 helper hash 下完成，但这不反向补齐未归档的测试。

这个无故障切片**没有杀任何进程**。`next_fault_eligible=true` 当时只表示后续可以另开切片去杀 exact child，不是
`reject_on_worker_lost`、requeue、redelivery、恢复时延或 exactly-once 的证据。证据收集后四个相关
服务以 base Compose 重建；独立快照显示四者无 overlay，API / worker 的 ACK 设置恢复 false / false，
live Redis channel 恢复 3600 秒，worker 回到 gevent / max 4 / 无 OS child。

2026-09-01 的独立 child-loss 切片在新 WSL boot、新 sink container 和 fresh key 下重新建立全部门槛。
两个无效尝试被保留：一个在请求前遇到 client container 名称冲突，另一个因 helper 错把初始 Redis
delivery 的 `redelivered=null` 强制要求为 `false`，都 fail closed 且**没有 kill**。有效样本
`pub-prefork-child-loss-20260901-002` 在 kill 前同时归档了 attempt 1 已 `fsync`、release 不存在、
exact run/task active、`acknowledged=false / redelivered=false`、Redis queue 0 / unacked 1 / index 1，
以及 task `worker_pid` 同时命中唯一 pool process 和 direct OS child。orchestrator 随后立即重采
`(worker_container_id, child_container_pid, child_host_pid, child_host_start_ticks)`，确认 parent、
container 与 restart count 未变，只从 WSL host namespace 对该 `child_host_pid` 发 `SIGKILL`；没有杀
controller 或 container。归档 PID 只作历史证据，绝不能作为后续 fault target。

surviving parent 记录 `WorkerLostError` 后生成 replacement child；同一 task id 与 delivery tag 在新 child
上以 `redelivered=true` active，Redis 也为 `redelivered=true`，sink attempt 2 已落盘且 release 仍不存在。
第二次 delivery 比初始 visibility due 早约 **843.906 秒**，所以本样本隔离到了 parent-side
worker-loss requeue/redelivery，而不是 visibility-timeout restoration。host kill 返回到日志第二次 receive
约 0.116 秒只是跨时钟观测，不作因果 gate。只有上述 recovery gate 归档后才创建 release；最终同一
workflow run 以 3 steps 成功、Redis 清空，但 effect 为 **2 次**，数据库中原 effect row 仍为 `running`，
重放生成的 effect row 才 `succeeded`。结论仍是 **duplicate**，不是 exactly-once，也不是 node-row
reconciliation。

终态后再次 base-only 重建四服务和 `ssrf_proxy`：四服务 overlay mount 为 0，API / worker ACK 设置恢复
false / false，live Redis channel 恢复 3600 秒，worker 回到 gevent / max 4 / prefetch 4 / 无 OS child，
Redis queue/unacked/index 为 0；实验 client 与 sink 已移除，隔离 WSL 与 `Ubuntu` 的最终点时观察均为
`Stopped`，保留的 VHD、数据库、marker、release 和证据文件没有删除。

这些边界必须一起说：

- Dify 的 HTTP 节点确实提供重试和错误处理配置；人工输入节点也提供暂停、表单提交与恢复。
  但重试的单位是整个节点，不替调用方解决副作用幂等。官方 CLI 文档也明确提醒：对可能已开始执行的
  POST 自动重试并不安全。
- 杀 `api` 不等于杀 worker，杀 worker 也不一定命中 executor。debugger 的 worker 故障样本在约
  14 分 24 秒内没有重复副作用也没有恢复；Published `blocking` 则根本不走该 Celery task。
- 有效的 Published `streaming` 故障样本证明 early ACK 后没有 Redis delivery 可供 visibility
  timeout 恢复；这描述的是实测配置语义，不是框架缺陷。
- late-ACK 故障样本证明本次 broker delivery 后来可重投，也证明了重投整个 workflow task 会重复
  已落盘的非幂等副作用；最终 `succeeded` 不能替代 task id、redelivery flag 和 effect count 证据。
- prefork 正常 control 只证明真实 child attribution、late-ACK 正常路径和可回滚性；后续独立 child-loss
  才证明这一个 surviving-parent 样本中的 replacement / same-task redelivery / duplicate recovery。
  它没有测 visibility-timeout latency，也不提供 exactly-once、一般 prefork 兼容性或生产保证。

对应官方文档：[HTTP Request](https://docs.dify.ai/en/cloud/use-dify/nodes/http-request)、
[错误处理](https://docs.dify.ai/en/cloud/use-dify/build/predefined-error-handling-logic)、
[Human Input](https://docs.dify.ai/en/cloud/use-dify/nodes/human-input)、
[POST 重试风险](https://docs.dify.ai/en/cli/integrate-agents/error-handling-and-retries-for-agents)、
[Celery task ACK 配置](https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-acks-late) 与
[Redis visibility timeout](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html#visibility-timeout)。

## 边界（面试时必须一起说）

- 这是**为了对齐语义写的对照实现**，不是生产系统，没有上线、没有真实用户。
- 副作用是本地文件写入，不是网络请求或数据库事务；真实分布式场景下的一致性问题比这里难。
- 只覆盖 LangGraph 的 StateGraph / checkpointer / interrupt 三块。
  LangChain 生态里的 RAG、向量库、Agent 预制件我仍然没有用过。
- 崩溃注入 30 次 × 20 步，规模比我自己项目里那个基准（30 × 100）小。
- Dify 只覆盖本地 1.16.1 的 debugger 草稿，以及独立 Compose 栈中的单 worker Published API
  `blocking` / `streaming`、early ACK、一次 experiment-only late ACK whole-container 对照和一次
  experiment-only prefork 无故障控制与一次 exact pool-child-only fault；没有覆盖集群、定时任务、
  生产流量、连续在线的 timeout redelivery latency 或 child-loss 结果分布。
  debugger worker 结论只适用于约 14 分 24 秒窗口，early-ACK Published fault 只适用于约 3 分钟窗口；
  late-ACK fault 的外部暂停边界必须与其冷启动重投结论一起说明。
