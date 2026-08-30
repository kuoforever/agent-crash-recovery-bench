# Dify 对照实验状态

**状态：Dify 1.16.1 的四个本地实验切片已经闭合。** 2026-08-04 的 debugger 草稿覆盖
HTTP 重试、Human Input、API 重启和一次 worker 硬崩溃；2026-08-28 的独立 Published API
数据栈补了 `blocking` / `streaming` executor attribution 与默认 early-ACK task 的硬崩溃；2026-08-30
又在同一隔离栈里完成 experiment-only late-ACK whole-container 对照，以及独立的 prefork feasibility +
no-fault normal-control 切片。
结果见根目录 `README.md`。debugger 的结构化记录在 `evidence/dify-semantics-report.json` 与
`evidence/dify-raw-snapshot.json`；Published API 记录在 `evidence/dify-published-crash-report.json`
与 `evidence/dify-published-crash-raw.json`；late-ACK 记录在
`evidence/dify-published-late-ack-report.json` 与 `evidence/dify-published-late-ack-raw.json`；prefork
无故障对照在 `evidence/dify-prefork-control-report.json` 与
`evidence/dify-prefork-control-raw.json`。

## 环境

| 项 | debugger 草稿（2026-08-04） | Published API（2026-08-28 / 30） |
|---|---|---|
| 运行位置 | WSL `Ubuntu`；Docker 28.2.2 + Compose v2.29.7 | 独立 WSL `DifyBench-Isolated-20260828`；Docker 29.1.3 + Compose v2.29.7 |
| 固定 checkout 语义参照 | WSL 内 `~/dify` | commit `5456d4d56e5701999bc8da2a2c97f5ae9b3b78d3`；不作为运行字节身份 |
| 运行镜像 | 本地 debugger Compose 镜像 | `langgenius/dify-api:1.16.1`；Published image ID `sha256:48295be…6362cb` |
| Compose project | `docker` | `dify_pub_20260828`，独立 DB / Redis / bind state |
| 入口 | Windows `http://localhost:8088` | Windows Nginx `http://127.0.0.1:18088`；streaming client 在 Compose network 直连 `http://api:5001` |
| 工作流 | 三个未发布草稿 App | 发布的 `dify/http-no-retry.yml` |
| 副作用 sink | `guarded_loop/dify_sink.py` | 同一实现，独立状态目录 |
| 凭据 | 不需要模型 API key | 不需要模型 API key；Published App token 只存在忽略目录，证据仅保存 SHA-256 前缀 |

Published 镜像与固定 checkout 的 `entrypoint.sh`、`celery_entrypoint.py` hash 相同，但 `ext_celery.py`
分别为 `b380…` 与 `0735…`，并不相同；controller / service / task 文件也没有逐文件 byte binding。
因此 checkout 只提供源码语义假设，executor 结论还必须由 worker log 与 active inspect 独立佐证。

prefork 的结构化 post-rollback 快照是在隔离服务仍运行时采集；随后 sink 被停止，两个 WSL 发行版也
停止。后一步是已转录进 prefork raw 的 host terminal observation，不是另一份容器 runtime JSON；VHD、
Compose bind/volume 和悬挂 run 状态仍保留。
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
| `pub-lateack-control-002` | experiment-only late ACK；`streaming` 正常控制 | `succeeded` / 3 steps | 1 | 阻塞时 `acknowledged=false`、Redis `unacked=1` |
| `pub-lateack-worker-crash-001` | experiment-only late ACK；effect 后杀整个 exact active worker 容器 | 冷启动重投后 `succeeded` / 3 steps | **2** | same task id + `redelivered=true`；判定 `duplicate` |
| `pub-prefork-control-003` | experiment-only prefork + late ACK；`streaming` 无故障控制 | `succeeded` / 3 steps | 1 | exact task PID 148 命中唯一 OS child、pool process 与 Redis unacked；内部 HTTP 200 后清零 |

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
- late-ACK 对照用 read-only `ext_celery.py` overlay，只对 exact workflow task 设置 effective
  `acks_late=true`、`reject_on_worker_lost=true`；broker、result backend、global 与 live Redis
  channel 的 transport-wide visibility timeout 均实测为 120 秒。正常控制在释放前保留一个 unacked delivery，
  释放后 effect 仍为 1 次且 unacked 清零。
- late-ACK fault 在 attempt 1 落盘后命中 exact active worker，active task 为 `acknowledged=false`；
  容器 exit 137 后 Redis delivery 仍在。外部暂停打断了连续 timeout 观察，因此不能断言 120 秒准时恢复。
  后续冷启动时同一 task id 以 `redelivered=true` 收到并产生 attempt 2；最终 run 虽为 `succeeded`，
  但原 effect node row 仍为 `running`，应用层判定必须是 `duplicate`。
- whole-container kill 没有留下 Celery parent 来执行即时 reject；本轮证明的是 Redis broker restoration，
  没有隔离证明 `reject_on_worker_lost`。三条 worker 崩溃结果都是本地单次、有界观察，不是 Dify 所有
  部署的恢复保证。
- prefork 切片的 API / worker 各自读取到 exact-task `acks_late=true`、`reject_on_worker_lost=true`，
  broker / result-backend / global / live-channel timeout 均为 900 秒；worker argv 和 stats 又独立证明
  prefork、concurrency 1、prefetch 1。application config 仍显示默认 prefetch 4，不能拿它替代 argv / stats。
- 两次请求前快照保持同一 worker、controller PID 1、唯一 direct child PID 148 与相同 start ticks；
  stats pool 为 `celery.concurrency.prefork:TaskPool`、processes `[148]`。启动仍运行 gRPC / psycopg2 的
  gevent-related patch，因此只证明这个本地 HTTP workflow 的可行性，不证明一般兼容性。
- 有效 prefork control 阻塞时 exact task `acknowledged=false / redelivered=false`、Redis queue 0 / unacked 1 /
  index 1，sink attempt 1 已落盘；orchestrator 创建 release 后内部 HTTP 是 200 且 body 为
  `released=true`，同一 run
  以 3 steps 完成、effect 仍为 1、Redis 全清、日志 received / succeeded 各一次且无 redelivery。
- 第一次 prefork control 的决定性失败证据是内部 Squid 504，未收到 sink origin
  `200 + released=true`；release 内容时间、NTFS stat 与 client wall / monotonic 读数不能可靠
  建立跨时钟先后，不作因果 gate。第二次在发请求前暴露 fail-closed client-name guard
  缺陷；orchestration 只记录 workflow request 未发出，没有失败后 DB / Redis / sink 快照或单独
  三路径测试 transcript。当前 hash-pinned helper 已改用 exit code，后续 control-003 在该 helper
  hash 下完成，但不反向补齐这些未归档证据。
- artifact 跨两次 WSL boot：最初 baseline / control-001 / 旧 rollback 在 `8b2dd1ae…`，control-002 /
  control-003 / 当次 rollback 在 `d54236db…`。只为 control-003 blocked → final 主张同 boot process
  continuity；rollback 是当次 boot 的 base / gevent 实测与旧 baseline 配置/拓扑等价。
- prefork 本轮没有注入故障。回滚以 base-only Compose 重建四个相关服务，独立保存四者无 overlay、
  API / worker ACK false / false、transport options 为空、live channel 3600，以及 worker gevent / max 4 /
  prefetch 4 / 无 OS child；`next_fault_eligible` 不是 recovery 结论。

## 本地复现要点

sink 必须和 Dify 在同一个 Docker 网络，并把仓库挂载为 `/bench`：

```bash
docker run -d --name dify-bench-sink --network docker_default \
  -v /path/to/agent-crash-recovery-bench:/bench -w /bench \
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
6. late-ACK 变体还要把 exact task id、delivery tag、visibility score / due 与 redelivery flag 对齐保存。

隔离栈第一次建网后曾出现 Docker `FORWARD` 默认 `DROP`、但新 bridge 未进入 `DOCKER-FORWARD`
规则的问题：DNS 能解析，新的 Redis TCP 连接却超时。`pub-stream-control-001` 因此被判无效；它没有
DB run、Celery task 或 sink event。只重启隔离发行版内 Docker daemon 后，先验证 bridge 规则、跨容器
Redis `PONG`、API healthy 和 worker ready，再用新 key 跑出有效 control / fault。不要把这个环境故障
算成 Dify 行为。

late-ACK 配置不是对 Dify checkout 或 image 的持久修改：overlay 覆盖运行镜像内 `ext_celery.py`，
runtime image base、固定 checkout 与 effective overlay 的 hash 分别为 `b380…`、`0735…`、`6ec8…`，
不能称为同一 revision 的字节身份。旧 late-ACK artifact 只独立归档 worker 的 post-rollback effective
设置；不能把它扩写成四类服务都有独立归档。后来的 prefork 切片另行归档了自己回滚后的四服务
allowlisted env、mount count 0、target hashes，以及 API / worker effective runtime；这提高的是新切片的
证据等级，不能反向升级旧 artifact。
`pub-lateack-control-001` 因 client container entrypoint 错误被判无效；它没有创建 run、task 或 effect，
也没有 Redis queue/unacked，不能算作框架行为。

## 仍需注意

- `crash-worker-001` 是有意留下的悬挂运行，用来保存崩溃证据；不是待恢复的业务任务。
- `pub-stream-worker-crash-001` 也有意保留为隔离 DB 中的悬挂 run；不要手工改成终态或重放。
- `pub-lateack-worker-crash-001` 已经由 broker 重投并收敛为 `succeeded`，但 sink 有两个 attempt，且
  数据库保留原 `running` effect row；不要把终态成功解释为 exactly-once，也不要手工清理证据行。
- HITL 重启前的 `paused / total_steps=2 / sink=0` 只保留了操作者观察，没有中间原始截图或查询 transcript；
  可独立复核的是表单创建、API 新进程启动、提交、effect 和最终状态的时间线。
- 旧 debugger worker 的精确 kill 时刻没有归档；新 Published fault 已保存 marker、active task、
  精确 kill / restart 时间、exit 137 与三阶段 Redis 快照。
- 三个 DSL 的 key 只用于实验 token，限定为 `[A-Za-z0-9_.-]{1,120}`；没有验证任意文本的 JSON escaping。
- Published API 已覆盖 `blocking` attribution、`streaming` 正常 control、默认 early-ACK whole-worker
  crash、一次 experiment-only late-ACK whole-worker crash，以及一次 experiment-only prefork 无故障
  control；没有覆盖仅杀 Celery pool child 来隔离 `reject_on_worker_lost`、集群 worker、定时任务、API
  executor crash、生产流量或连续在线的 timeout redelivery latency 测量。
- 新 no-fault prefork 切片已证明本地 runtime 存在一个可精确归因的 direct OS pool child，但从未杀过它。
  因此 child-loss 后由 parent 触发的即时 reject / requeue、replacement child、Redis visibility restoration、
  recovery latency 和 effect count 都仍未测；下一次 fault 必须作为独立切片重新建立全部门槛。
- sink 无认证，只能用于本机受控实验，不应暴露到公网。
