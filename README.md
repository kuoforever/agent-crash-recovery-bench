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
| `evidence/` | 原始数据：崩溃注入报告、真实模型 trace，以及 Dify debugger / Published API 摘要与脱敏快照 |

## 跑什么

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-lock.txt
python -m guarded_loop.crash_bench --runs 30 --steps 20 --out _bench   # 崩溃注入基准
python -m guarded_loop.eval_trace                                       # 10 个确定性 case
python -m guarded_loop.llm_run                                          # 真实模型链路（需 OPENAI_API_KEY）
```

前两条不需要网络，也不需要 API key。

## 结论一：LangGraph 的检查点给的是"能接着走"，不是"不会重复做"

崩溃注入方式与我自己项目里那套一致：子进程跑到指定步骤 `os._exit(70)` 硬退出（不跑 finally，真崩溃不给清理机会），
父进程用同一个 `thread_id` 恢复，最后数落地的副作用有没有重复。30 次运行 × 每次 20 步，崩溃 30/30 全部确认。

| 对照组 | 有重复副作用的运行 | 重复副作用条数 | 停止码 |
|---|---|---|---|
| 纯检查点 · `durability="async"`（默认） | 29 / 30 | **128** | 全部静默跑完 |
| 纯检查点 · `durability="sync"`（最强持久化） | 20 / 30 | **20** | 全部静默跑完 |
| 检查点 + 自建意图账本 | **0 / 30** | **0** | 20 次 `UNCERTAIN_HALT` / 10 次正常 |

数字能逐条对上账，这点比数字本身重要：

- `sync` 下每次崩溃只重放**正在执行的那个节点**，所以恰好 1 条重复 × 20 次运行；
  `async` 下已完成但未落盘的 superstep 也会一起丢，所以重复条数涨到 128。
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

10 个确定性 case，判的是**调用序列 + 停止码 + 副作用条数**，不判模型说了什么。
期望值用 SHA-256 冻进 `eval_manifest.json`（当前 `fa9c9eed8e15387b...`），
改实现时如果顺手把不过的用例调松，manifest 对不上会直接报出来。当前 10/10 通过。

覆盖：注册表外的工具名被拒 / 参数不合契约不进执行层 / 超长文本被挡 / 重放不产生第二次副作用 /
留下未完成意图判不确定 / 正常回路副作用条数正确 / 高风险动作挂起且未落副作用 /
审批被拒不执行 / 审批通过恰好执行一次 / 不确定停机后不推进游标。

## 真实模型链路

`ChatOpenAI(gpt-4o-mini) + bind_tools`，固定注册表原样暴露给模型，
但**执行不交给 LangChain 的 ToolNode**——模型给的参数一样要逐字段过 pydantic schema 才准进执行层。
实跑序列：`read_status(t0)` → `write_note(t0, "hello")` → 收尾，两次调用都 `ok`。

崩溃基准刻意不接模型：恢复语义要单独量，模型的随机性混进去就成噪声了。

## Dify 1.16.1 对照：执行模式先决定谁是 executor

本地 Docker Compose 版 Dify 1.16.1 上先跑了 debugger 草稿，再在独立 WSL / Compose 数据栈中
发布同一条 HTTP 工作流。HTTP sink 会先 `fsync` 副作用记录，再返回指定状态码；也可以在落盘后、
返回前阻塞，给硬杀进程留出确定窗口。

debugger 结论在 `evidence/dify-semantics-report.json`，原始快照在
`evidence/dify-raw-snapshot.json`；Published API 结论在
`evidence/dify-published-crash-report.json`，原始快照在
`evidence/dify-published-crash-raw.json`。可导入 DSL 在 `dify/`。实验 key 限定为
`[A-Za-z0-9_.-]{1,120}`，不把任意用户文本直接当作 JSON 测试输入。

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

Dify 1.16.1 源码和实跑互相对上：Published workflow 的 `blocking` 分支在 API 进程内启动
Python thread；`streaming` 分支才把 `workflow_based_app_execution_task` 投递到 Celery worker。
因此“杀了 worker”之前必须先证明目标 run 确实在那个 worker 上 active。

| 场景 | 最终/快照状态 | 副作用次数 | 实测语义 |
|---|---:|---:|---|
| `blocking`；effect 后杀 Celery worker | `succeeded` / 3 steps | 1 | 没命中 executor；外层 API 200，HTTP 节点把 Squid 504 当输出 |
| `streaming` 正常控制 | `succeeded` / 3 steps | 1 | exact run id 出现在 Celery task，证明 streaming 执行边界 |
| `streaming`；effect 后杀 active worker | 约 3 分钟快照仍 `running` / 0 steps | 1 | task 已确认；无 queue/unacked、无重投、无应用层收敛 |

正式故障样本 `28e83e19-b8af-4344-8421-0b887b27712a` 在 kill 前同时满足三项归因：sink
已有 `attempt=1`；worker log 含 exact run id；`celery inspect active` 显示该 task 在目标 worker
上且 `acknowledged=true`。目标容器随后以 137 退出并显式重启。运行时复核得到 effective
`task_acks_late=false`、`task_reject_on_worker_lost=false`；kill 前、kill 后和重启后的 Redis
queue、`unacked`、`unacked_index` 都为空。

所以这次不该“继续等 visibility timeout”：Redis visibility timeout 处理的是未确认 delivery，
而这条消息在 worker 崩溃前已经确认。约 3 分钟的有界观察内没有第二次 effect，代价是数据库 run
和 HTTP 节点仍停在 `running`，SSE 客户端没有收到终态。它说明的是这一个配置下没有 broker
恢复入口，不是 Dify 所有部署永远不会恢复。

三条边界必须一起说：

- Dify 的 HTTP 节点确实提供重试和错误处理配置；人工输入节点也提供暂停、表单提交与恢复。
  但重试的单位是整个节点，不替调用方解决副作用幂等。官方 CLI 文档也明确提醒：对可能已开始执行的
  POST 自动重试并不安全。
- 杀 `api` 不等于杀 worker，杀 worker 也不一定命中 executor。debugger 的 worker 故障样本在约
  14 分 24 秒内没有重复副作用也没有恢复；Published `blocking` 则根本不走该 Celery task。
- 有效的 Published `streaming` 故障样本证明 early ACK 后没有 Redis delivery 可供 visibility
  timeout 恢复；这描述的是实测配置语义，不是框架缺陷。

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
- Dify 只覆盖本地 1.16.1 的 debugger 草稿，以及独立 Compose 栈中的单次 Published API
  `blocking` / `streaming` 对照；没有覆盖集群、定时任务、late-ACK 配置或生产流量。debugger
  worker 结论只适用于约 14 分 24 秒窗口，Published streaming worker 结论只适用于约 3 分钟窗口。
