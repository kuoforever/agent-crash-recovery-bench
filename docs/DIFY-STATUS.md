# Dify 对照实验状态

**结论：Dify 1.16.1 本地对照已经跑完。** 管理员初始化、三个工作流导入、HTTP/HITL、
API 重启和 worker 硬崩溃都已有实测证据。结果见根目录 `README.md`，结构化记录见
`evidence/dify-semantics-report.json`；逐事件、数据库行、脱敏 I/O、容器启动日志与故障注入 transcript
见 `evidence/dify-raw-snapshot.json`。

## 环境

| 项 | 当前状态 |
|---|---|
| 运行位置 | WSL Ubuntu；Docker 28.2.2 + Compose v2.29.7 |
| Dify 仓库 | WSL 内 `~/dify` |
| Dify 版本 | 1.16.1 |
| Windows 入口 | `http://localhost:8088` |
| 工作流 | 三个草稿 App，DSL 在仓库 `dify/` |
| 副作用 sink | `guarded_loop/dify_sink.py`，运行数据写到忽略提交的 `_dify_bench/` |
| 模型 API key | 不需要；本实验只有 HTTP Request 与 Human Input 节点 |

Windows 最初访问失败不是 Dify 端口配置，而是 WSL 在没有前台会话时很快自动停止。
实验期间用一个隐藏的 `wsl.exe -d Ubuntu` 进程保持发行版存活；它不是持久服务配置，重启机器后需重新启动。

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

## 实测结果

| 输入 key | 故障/配置 | 运行结果 | sink 记录 |
|---|---|---|---:|
| `no-retry-500-002` | 返回 500；不重试 | `succeeded` | 1 |
| `retry-500-001` | 返回 500；重试 3 次 | `failed` | 4 |
| `hitl-002` | 暂停后重启 API，再批准 | `stopped`；表单已提交 | 1 |
| `crash-mid-node-001` | 落盘后杀 API | `succeeded` | 1 |
| `crash-worker-001` | 落盘后杀 worker | 约 14 分 24 秒后的快照仍 `running` | 1 |

关键解释：

- 关闭重试和错误策略时，HTTP 500/502 会作为节点输出继续向后传，工作流仍可标成成功。
- 开启 3 次重试时，初次请求加 3 次重试共执行 4 次；每次请求前一个副作用都已经落盘。
- Human Input 的等待记录和表单在 API 容器重启后仍存在，批准后 worker 能恢复并执行副作用；
  但本次原 debugger SSE 响应流未续接，最终运行记录被标成 `stopped`。
- API 不是工作流执行器；worker 才是。worker 在副作用已落盘、HTTP 尚未返回时被 `docker kill`
  后，这一次运行在约 14 分 24 秒的观察窗口内没有重新投递，数据库里的运行和 HTTP 节点保持 `running`。
  这是单次、有时限的 debugger 观察，不是 Dify 所有部署的恢复保证。

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

## 仍需注意

- `crash-worker-001` 是有意留下的悬挂运行，用来保存崩溃证据；不是待恢复的业务任务。
- HITL 重启前的 `paused / total_steps=2 / sink=0` 只保留了操作者观察，没有中间原始截图或查询 transcript；
  可独立复核的是表单创建、API 新进程启动、提交、effect 和最终状态的时间线。
- worker 的精确 kill 时刻没有归档；快照保留了 effect 时间、worker 重连时间、退出码 137 与终端状态行。
- 三个 DSL 的 key 只用于实验 token，限定为 `[A-Za-z0-9_.-]{1,120}`；没有验证任意文本的 JSON escaping。
- 三个 App 都是未发布的 debugger 草稿。发布后的 API 调用、集群 worker、broker visibility timeout
  和更长时间的延迟恢复没有覆盖。
- sink 无认证，只能用于本机受控实验，不应暴露到公网。
