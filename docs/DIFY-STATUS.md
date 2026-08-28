# Dify 对照实验状态

**状态：Dify 1.16.1 的两个本地实验切片已经闭合。** 2026-08-04 的 debugger 草稿覆盖
HTTP 重试、Human Input、API 重启和一次 worker 硬崩溃；2026-08-28 的独立 Published API
数据栈补了 `blocking` / `streaming` executor attribution，以及 exact active Celery task 的硬崩溃。
结果见根目录 `README.md`。debugger 的结构化记录在 `evidence/dify-semantics-report.json` 与
`evidence/dify-raw-snapshot.json`；Published API 记录在 `evidence/dify-published-crash-report.json`
与 `evidence/dify-published-crash-raw.json`。

## 环境

| 项 | debugger 草稿（2026-08-04） | Published API（2026-08-28） |
|---|---|---|
| 运行位置 | WSL `Ubuntu`；Docker 28.2.2 + Compose v2.29.7 | 独立 WSL `DifyBench-Isolated-20260828`；Docker 29.1.3 + Compose v2.29.7 |
| Dify 源码 | WSL 内 `~/dify` | 1.16.1，commit `5456d4d56e5701999bc8da2a2c97f5ae9b3b78d3` |
| Compose project | `docker` | `dify_pub_20260828`，独立 DB / Redis / bind state |
| 入口 | Windows `http://localhost:8088` | Windows Nginx `http://127.0.0.1:18088`；streaming client 在 Compose network 直连 `http://api:5001` |
| 工作流 | 三个未发布草稿 App | 发布的 `dify/http-no-retry.yml` |
| 副作用 sink | `guarded_loop/dify_sink.py` | 同一实现，独立状态目录 |
| 凭据 | 不需要模型 API key | 不需要模型 API key；Published App token 只存在忽略目录，证据仅保存 SHA-256 前缀 |

两个 WSL 发行版在证据收集后都已停止；VHD、Compose bind/volume 和悬挂 run 状态仍保留。
Published 实验用独立 VHD 与 Compose project，未复用旧 debugger DB/Redis。隔离预检曾意外短暂启动原
`Ubuntu`；据本轮操作者即时只读观察，旧 run / effect 未变，随后终止，但该中间观察没有独立归档。
这个证据等级与限制也写入新 raw snapshot。

## 导入的工作流

| DSL | 本地 App id | 作用 |
|---|---|---|
| `dify/http-no-retry.yml` | `d2cd725c-78e3-4669-b7d7-6bc0273856c8` | 500、不重试与崩溃窗口 |
| `dify/http-retry-3.yml` | `38c669d6-7e99-4789-b77e-7f944502e6b3` | 失败后最多重试 3 次 |
| `dify/hitl-before-effect.yml` | `d52db1de-15b2-42d6-8d5f-85d047ee84b2` | 审批完成后才执行副作用 |

HITL DSL 最初使用了非 UUID 的 delivery method id，Dify 导入后无法运行。仓库 DSL 已改成固定 UUID；
已导入的本地草稿则直接在 PostgreSQL 的 draft workflow JSON 中只改了这一处字段：
workflow id `1b16548c-ada4-41ed-80f0-c438164b3fb6` 的
`nodes[1].data.delivery_methods[0].id`。这是本地实验修复，不应当当成通用迁移步骤。

Published App id 为 `4a820f63-bd4b-47dd-9854-f36751116d8e`，workflow id 为
`249f4342-8fd8-4def-a464-15ee54426e6e`。App token 明文没有进入仓库。

## Debugger 实测结果

| 输入 key | 故障/配置 | 运行结果 | sink 记录 |
|---|---|---|---:|
| `no-retry-500-002` | 返回 500；不重试 | `succeeded` | 1 |
| `retry-500-001` | 返回 500；重试 3 次 | `failed` | 4 |
| `hitl-002` | 暂停后重启 API，再批准 | `stopped`；表单已提交 | 1 |
| `crash-mid-node-001` | 落盘后杀 API | `succeeded` | 1 |
| `crash-worker-001` | 落盘后杀 worker | 约 14 分 24 秒后的快照仍 `running` | 1 |

## Published API 实测结果

| 输入 key | 模式/故障 | 运行结果 | sink 记录 | 归因 |
|---|---|---|---:|---|
| `pub-control-001` | `blocking` 正常控制 | `succeeded` / 3 steps | 1 | API 进程内执行 |
| `pub-worker-crash-001` | `blocking`；effect 后杀 Celery worker | `succeeded` / 3 steps；HTTP node 504 | 1 | **未命中 executor**，只算 attribution control |
| `pub-stream-control-002` | `streaming` 正常控制 | `succeeded` / 3 steps | 1 | exact run 进入 Celery task |
| `pub-stream-worker-crash-001` | `streaming`；effect 后杀 exact active worker | 约 3 分钟快照仍 `running` / 0 steps | 1 | worker log + `celery inspect active` 双重命中 |

关键解释：

- 关闭重试和错误策略时，HTTP 500/502 会作为节点输出继续向后传，工作流仍可标成成功。
- 开启 3 次重试时，初次请求加 3 次重试共执行 4 次；每次请求前一个副作用都已经落盘。
- Human Input 的等待记录和表单在 API 容器重启后仍存在，批准后 worker 能恢复并执行副作用；
  但本次原 debugger SSE 响应流未续接，最终运行记录被标成 `stopped`。
- 对旧 debugger 路径，API 不是工作流执行器，worker 才是；该样本约 14 分 24 秒内没有重投或收敛。
- 对 Published workflow，`response_mode` 改变 executor 边界：Dify 1.16.1 的 `blocking` 分支在 API
  进程内开 Python thread，`streaming` 分支才投递 `workflow_based_app_execution_task` 到 Celery。
- 有效 streaming fault 在 kill 前保存了 exact run 的 worker log 与 active task；active record 明确为
  `acknowledged=true`。运行时 effective `task_acks_late=false`、`task_reject_on_worker_lost=false`，
  kill 前/后/重启后 Redis queue 与 unacked 都为 0，所以 visibility timeout 对这条已确认 delivery
  不构成恢复入口。约 3 分钟内没有第二次 effect，也没有把 run 从 `running` 收敛到终态。
- 两条崩溃结果都是单次、有时限的本地观察，不是 Dify 所有部署的恢复保证。

## 本地复现要点

sink 必须和 Dify 在同一个 Docker 网络，并把仓库挂载为 `/bench`：

```bash
docker run -d --name dify-bench-sink --network docker_default \
  -v /mnt/c/Users/Alienware/agent-crash-recovery-bench:/bench -w /bench \
  langgenius/dify-api:1.16.1 \
  python -m guarded_loop.dify_sink --host 0.0.0.0 --port 8099 \
  --state-dir /bench/_dify_bench/sink
```

Dify 的 SSRF 代理默认拒绝私网目标。本次只为本地实验重建了 `ssrf_proxy` 容器并放行 sink 主机名：

```bash
cd ~/dify/docker
export SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=dify-bench-sink
docker compose up -d --force-recreate ssrf_proxy
```

这是容器级临时状态；后续不带这个环境变量重建 `ssrf_proxy` 会恢复默认拒绝。不要把整个私网段加入白名单。

Published fault 的 kill 前硬门槛比旧实验更严格：

1. 请求必须明确为 Published Service API `response_mode=streaming`。
2. sink marker 的 key 必须与数据库 run 输入一致。
3. worker log 必须含 exact `workflow_run_id`。
4. `celery inspect active` 必须同时显示 exact run、目标 task、目标 hostname 与 `acknowledged` 状态。
5. 保存目标 container id、Redis queue/unacked 快照后才允许 kill。

隔离栈第一次建网后曾出现 Docker `FORWARD` 默认 `DROP`、但新 bridge 未进入 `DOCKER-FORWARD`
规则的问题：DNS 能解析，新的 Redis TCP 连接却超时。`pub-stream-control-001` 因此被判无效；它没有
DB run、Celery task 或 sink event。只重启隔离发行版内 Docker daemon 后，先验证 bridge 规则、跨容器
Redis `PONG`、API healthy 和 worker ready，再用新 key 跑出有效 control / fault。不要把这个环境故障
算成 Dify 行为。

## 仍需注意

- `crash-worker-001` 是有意留下的悬挂运行，用来保存崩溃证据；不是待恢复的业务任务。
- `pub-stream-worker-crash-001` 也有意保留为隔离 DB 中的悬挂 run；不要手工改成终态或重放。
- HITL 重启前的 `paused / total_steps=2 / sink=0` 只保留了操作者观察，没有中间原始截图或查询 transcript；
  可独立复核的是表单创建、API 新进程启动、提交、effect 和最终状态的时间线。
- 旧 debugger worker 的精确 kill 时刻没有归档；新 Published fault 已保存 marker、active task、
  精确 kill / restart 时间、exit 137 与三阶段 Redis 快照。
- 三个 DSL 的 key 只用于实验 token，限定为 `[A-Za-z0-9_.-]{1,120}`；没有验证任意文本的 JSON escaping。
- Published API 已覆盖一次 `blocking` attribution control、一次 `streaming` 正常 control 和一次
  exact active worker crash；没有覆盖 late ACK / reject-on-worker-lost 变体、集群 worker、定时任务、
  API executor crash 或更长时间的应用层 reconciliation。
- sink 无认证，只能用于本机受控实验，不应暴露到公网。
