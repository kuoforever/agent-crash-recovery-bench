# 交接说明

给下一个接手的人。读这份就够，不需要翻聊天记录。

## 现在是什么状态

**已完成**：LangGraph / LangChain 的对照实现与三组实验；Dify 1.16.1 的 debugger HTTP 重试、
Human Input、API 重启和 worker 崩溃；以及独立 Published API `blocking` / `streaming` executor
attribution、默认 early-ACK worker 崩溃与 experiment-only late-ACK whole-worker 对照。结论见根目录 `README.md`，设计理由见
`docs/DESIGN.md`，原始数据在 `evidence/`。

一句话版本：LangGraph 的检查点给的是"能接着走"不是"不会重复做"；崩溃注入 30 次 × 20 步，
纯检查点默认配置下重复副作用 128 条、最强持久化 `sync` 下 20 条、叠加自建两段式意图账本后 0 条。

Dify 一句话版本：debugger 不重试时 HTTP 500 被当作普通输出，配置 3 次重试会把已落盘副作用
做 4 次；Published workflow 的 `blocking` 在 API 进程内执行，`streaming` 才进入 Celery。有效
streaming fault 中，exact active task 已 `acknowledged=true` 后 worker 以 137 退出；约 3 分钟内
effect 仍为 1 次，但 run / HTTP node 停在 `running`，Redis 没有 queue 或 unacked delivery 可供
visibility timeout 恢复。只给 exact workflow task 开 late ACK 的对照则保住了 Redis unacked delivery；
后续冷启动时 same task id 以 `redelivered=true` 重投，run 最终成功但 effect 变成 2 次，判定
`duplicate`。环境、run id 与脱敏快照见 `docs/DIFY-STATUS.md` 以及六个 `evidence/dify-*.json`。

证据边界：旧 debugger 的 HITL 重启前暂停状态和 worker 精确 kill 时刻没有独立截图/时间戳；
Published fault 则保留了 marker、exact active task、精确 kill / restart 时间、exit 137、Redis 三阶段
快照和约 3 分钟终态快照。late-ACK fault 又保存了原 delivery、cold-start same-task redelivery、
第二次 effect 与最终 node rows；但中间有外部 turn / WSL 暂停，不能声称恰好在 120 秒重投。
三者都是本地单次有界观察，不得互相补证或泛化。

## 环境与复现

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-lock.txt

python -m guarded_loop.crash_bench --runs 30 --steps 20 --out _bench
python -m guarded_loop.eval_trace
python -m guarded_loop.llm_run          # 需要 OPENAI_API_KEY
```

前两条不需要网络也不需要 API key，可离线重跑。第三条会真的调 `gpt-4o-mini`。

开发时用的是 Python 3.13.7 / Windows。版本锁在 `requirements-lock.txt`。

Dify 两个 WSL 发行版当前都已停止，数据仍保留。Published 栈位于独立发行版
`DifyBench-Isolated-20260828`、Compose project `dify_pub_20260828`；恢复它之前先检查 Docker
`DOCKER-FORWARD` 是否含新 bridge，并从同网容器验证 Redis `PONG`。原 `Ubuntu` 中的
`crash-worker-001` 和隔离 DB 中的 `pub-stream-worker-crash-001` 都是有意保留的悬挂证据，不要重放、
手工改终态或混入下一次 key。late-ACK overlay 清理与无 override 重建动作已执行；保留的 worker
post-rollback 快照显示 effective task 设置恢复为 false/false、transport options 为空、live Redis
channel 恢复默认 3600 秒，但 API / websocket / beat 没有等价的独立归档快照。不要假设重启后仍是实验配置。

## 代码结构

```
worker.py        一次运行 = 一个子进程（崩溃注入靠杀进程，不靠抛异常）
  └─ graph.py    LangGraph StateGraph：plan → gate → act → route
       └─ tools.py   固定注册表 + pydantic 双向校验 + 意图账本（不 import 任何 langgraph 符号）
crash_bench.py   三组对照的崩溃注入基准
eval_trace.py    10 个确定性 case + SHA-256 manifest
llm_run.py       真实模型链路
dify_sink.py     Dify 对照用 HTTP sink：fsync、可控状态码、可阻塞崩溃窗口
dify/            三个可导入的 Dify DSL
evidence/dify-published-crash-*.json   Published API executor / worker-crash 证据
evidence/dify-published-late-ack-*.json   Published API late-ACK redelivery / duplicate 证据
```

`tools.py` 不依赖 LangGraph 是有意的——只有契约层能独立存在，
"哪些保证是框架给的、哪些是自己给的"才问得出来。改代码时请保持这条边界。

## 唯一下一动作

只在 `DifyBench-Isolated-20260828` 中做一个 **prefork feasibility + normal-control** 切片，先不注入故障。
固定 revision 的 worker entrypoint 默认 `CELERY_WORKER_POOL=gevent`，本轮 `Dummy-*` 执行上下文也没有
证明存在可单杀的 OS pool child。用 experiment-only override 把现有单 worker 显式改为 `prefork`、
concurrency 1，同时保留 exact-task late ACK annotation，并把 visibility timeout 设成长于整个控制窗口。
启动后必须保存最终 worker argv、`celery inspect stats` 的 pool 实现、parent/child 进程树，以及 exact
streaming task 的 child PID；再用新 key 跑一次阻塞后释放的正常 control，证明 run 成功、effect=1、
unacked 清零。Dify 的 `celery_entrypoint.py` 仍会 patch gevent 相关库，所以 pool 变化是新增实验变量；
若没有真实 OS child 或正常 control 不稳定，立即停止，不做 kill。只有这个切片通过后，下一轮才可用
长 visibility timeout 杀 exact child 来隔离 `reject_on_worker_lost`。

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
  Human Input、本地 debugger 草稿和单 worker Published API `blocking` / `streaming`、early ACK 与一次
  experiment-only late ACK whole-container 对照；prefork control、pool-child-only reject 隔离、连续 timeout latency、
  集群、定时任务、生产流量与 Coze 等其他平台仍是空白。
- 本仓库的数字（30×20、128/20/0、10 个 case）与 Guarded Desktop Agent 的数字
  （30×100、1420 项测试、13 个 case）是两套独立实验，不要混用。
