# 设计说明：这套回路为什么是这个形状

读这份文档前先读根目录 `README.md`——那里是结论和数字，这里是为什么。

## 出发点

我原本有一套自研的桌面 Agent 执行 runtime（Guarded Desktop Agent）。补 LangGraph 的目的不是学一个新 API，
而是回答一个具体问题：**同一条受控执行回路，交给框架去做，哪些保证还在、哪些没了、代价是什么。**

所以对照的四条约束是从自研那套里原样搬过来的，不是为了配合 LangGraph 现有能力挑的：

1. 工具集固定，参数与结果双向校验
2. 结果判三态，不确定不重放
3. 崩溃后能从检查点接着走
4. 高风险动作先过人工审批

## 分层

```
worker.py        一次运行 = 一个子进程（崩溃注入靠杀进程，不靠抛异常）
  └─ graph.py    LangGraph StateGraph：plan → gate → act → route
       └─ tools.py   固定注册表 + pydantic 双向校验 + 意图账本（不依赖 LangGraph）
```

`tools.py` 刻意不 import 任何 langgraph 符号。这条边界是整个对照实验的前提——
只有当契约层能独立存在，"哪些保证是框架给的、哪些是我自己给的"才问得出来。

## 三态语义

判据不是"有没有抛异常"，是**副作用到底发生没发生、我知不知道**：

| 态 | 含义 | 允许的后续动作 |
|---|---|---|
| `ok` | 拿到回执，副作用确定发生 | 继续 |
| `failed` | 工具明确拒绝，且保证没有副作用 | 安全中止；可重试 |
| `uncertain` | 派发出去了但没有回执 | **只能停机**，绝不重放 |

`uncertain` 是整套设计的核心。大多数框架的默认重试逻辑把它当 `failed` 处理，
于是"付了款但没收到回执"会变成"再付一次"。

## 意图账本为什么是两段式

```
mark_pending(key)  →  执行副作用  →  mark_done(key, receipt)
```

崩溃可能落在三个位置：

- 落在 `mark_pending` 之前：账本干净，恢复后重跑，正确。
- 落在 `mark_pending` 与副作用之间：账本有 pending 但副作用没发生。恢复后判 `uncertain` **停机——这是误停**，
  代价见 README（30 次里占 10 次）。
- 落在副作用与 `mark_done` 之间：账本有 pending 且副作用已发生。恢复后判 `uncertain` 停机，**这次是对的**。

后两种从账本上看完全一样，分不开。要分开必须让账本写入与副作用落在同一个事务里，
文件型 sink 做不到；数据库型副作用可以（把账本表和业务表放一个事务）。
**这是这套设计已知的、没有解决的限制**，不要在面试里说成"已经解决"。

## 崩溃注入方法学

```python
def maybe_crash(phase, step):
    if os.environ.get("GL_CRASH") == f"{step}:{phase}":
        os._exit(70)
```

用 `os._exit` 而不是 `raise`：真崩溃不跑 `finally`、不跑 `atexit`、不刷缓冲区。
用 `raise` 注入会让 Python 的清理逻辑替你把状态收拾干净，测出来的恢复能力是虚的。

三个注入相位对应副作用的三个时间窗：

- `pre_apply`：参数已校验、账本已记 pending、副作用尚未发生
- `post_apply`：副作用已落地、结果尚未校验、账本尚未标 done ← **最危险的窗口**
- `post_commit`：全部完成、节点尚未返回

`post_commit` 值得单独测：节点没返回就没有提交点，LangGraph 恢复时仍会重跑整个节点——
这一相位专门用来证明"做完了"和"框架知道你做完了"是两件事。

## 评测为什么判 trace 不判自然语言

模型输出的自然语言不稳定，拿它当判据的评测会一直在治标。所以 15 个 case 判三样东西：
**调用序列、停止码、副作用条数**。这三样都是确定性的。

`eval_manifest.json` 用 SHA-256 同时冻结期望值与受保护实现。评测体系最典型的死法不是没人跑，
是改实现时顺手把不过的用例调松了——manifest 对不上会直接报出来；manifest 缺失也不会自动创建，
改判据或实现必须先复核，再显式 `--update-manifest`。

## 故障编排与公开证据为什么分层

真实 fault 的危险动作不应和证据格式耦在一支脚本里。`fault_harness.py` 因此只保留可注入 adapter 的
state machine 与 atomic transcript：observation 先归档，gate 再判定，最后才可能调用 fault/release。
checked-in adapter 只能 replay fixture，不知道 Dify、容器、host namespace 或系统 kill 命令。未来若有
新的 live scope，平台 adapter 也必须在仓库外显式提供有界 capture/fault/release；state machine 本身
不会从历史 transcript 猜 PID，也不会把不完整 receipt 当成成功。

fault outcome 与工具三态一样不能压成 bool：adapter 返回 exact receipt 才是 `applied`，明确未执行才是
`not_applied`，异常或 identity 不一致必须是 `unknown`。release 也携带 `run_id / task_id / delivery_tag`
binding 并核对 receipt；invalid blocked/pre-fault identity gate 不做“善意 cleanup”，因为那可能释放另一条
任务，只能报 `cleanup_authority_unknown`。当前 generic replay 的边界是 capture count，不含可信 wall clock
或 `visibility_due`，所以 exhaustion 只叫 `redelivery_not_observed_within_capture_budget`。

即使 capture budget 已耗尽，cleanup 也不会依赖旧的 blocked delivery。latest observation 必须重新证明
exact active/unacknowledged task、same delivery、broker `0/1/1`、仅 attempt 1、replacement child 与 release
absent；task/broker 已清空、identity/count 改变、出现 attempt 2 或意外 release 时均 fail closed 为
`cleanup_authority_unknown`，且不发送 release。

证据层再拆成三个独立判据：JSON Schema 判结构，sanitizer 判公开内容，verifier 判跨文件 linkage 与
artifact availability。它们不能互相替代：manifest 中有 hash 不代表本机原件存在；schema valid 不代表
内容没有 secret；sanitized transcription 也不证明未跟踪原件的 hash。verifier 因而有三种 bundle 状态：

| 状态 | 含义 |
|---|---|
| `verified` | schema/linkage 均通过，所有 declared source 在本次 root 中可读且 hash/bytes 匹配 |
| `partial` | schema/linkage 通过，但明确声明为 `local_only` 的 source 不在 clone；逐项记 `unavailable` |
| `failed` | tracked source 缺失、hash/linkage/schema/count/policy/path 任一失败 |

`--allow-unavailable` 只改变 CLI 是否接受已声明的 `partial` 作为自动化边界，不改变 JSON 状态，也不把
33 个本机原件升级成已验证。sanitizer 是规则驱动的第二道防线，不是任意 secret 的形式化证明。
敏感 exact key 的非 null 值一律替换，audit 只认可整个值恰为 `[REDACTED]`；字符串中夹带该 marker
不会让 Bearer、credential assignment 或其余内容跳过扫描。

## 已知限制

- 只覆盖 LangGraph 的 StateGraph / checkpointer / interrupt 三块。子图、`Send`、流式、
  多智能体编排都没碰。
- 副作用是本地文件写入。真实场景里的网络请求、数据库事务、第三方 API 的一致性问题都更难。
- 崩溃注入 30 次 × 20 步，规模小于自研那套的 30 × 100。
- 没有并发：所有实验都是单线程单进程串行。并发下账本需要加锁，未验证。
- `plan_node` 是确定性的空实现。真实计划由模型生成的那条链路在 `llm_run.py` 里单独验，
  两者没有合并——合并会让崩溃恢复的测量混入模型随机性。
