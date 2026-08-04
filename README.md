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
| `docs/DIFY-STATUS.md` | Dify 已装好但卡住，卡在哪、怎么解 |
| `evidence/` | 原始数据：崩溃注入报告、真实模型 trace |

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

## 边界（面试时必须一起说）

- 这是**为了对齐语义写的对照实现**，不是生产系统，没有上线、没有真实用户。
- 副作用是本地文件写入，不是网络请求或数据库事务；真实分布式场景下的一致性问题比这里难。
- 只覆盖 LangGraph 的 StateGraph / checkpointer / interrupt 三块。
  LangChain 生态里的 RAG、向量库、Agent 预制件我仍然没有用过。
- 崩溃注入 30 次 × 20 步，规模比我自己项目里那个基准（30 × 100）小。
