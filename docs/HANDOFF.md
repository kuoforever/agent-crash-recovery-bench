# 交接说明

给下一个接手的人。读这份就够，不需要翻聊天记录。

## 现在是什么状态

**已完成**：LangGraph / LangChain 的对照实现与三组实验，以及 Dify 1.16.1 的 HTTP 重试、
Human Input、API 重启和 worker 崩溃对照。结论见根目录 `README.md`，设计理由见
`docs/DESIGN.md`，原始数据在 `evidence/`。

一句话版本：LangGraph 的检查点给的是"能接着走"不是"不会重复做"；崩溃注入 30 次 × 20 步，
纯检查点默认配置下重复副作用 128 条、最强持久化 `sync` 下 20 条、叠加自建两段式意图账本后 0 条。

Dify 一句话版本：不重试时 HTTP 500 被当作普通输出并成功结束；配置 3 次重试会把已落盘副作用
做 4 次；本次 Human Input 状态跨 API 重启后恢复，但原 debugger 响应流未续接；硬杀 worker 后
约 14 分 24 秒内没有观察到重投，运行记录停在 `running`。这是单次、有时限的本地观察。
环境、精确 run id 与脱敏原始快照见 `docs/DIFY-STATUS.md`、`evidence/dify-semantics-report.json`
和 `evidence/dify-raw-snapshot.json`。

证据边界：HITL 重启前的暂停状态和 worker 的精确 kill 时刻没有保存独立原始截图/时间戳；前者标成
operator-observed，后者只保留 effect 与 worker 重连之间的时间边界、退出码和终端 transcript。

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
```

`tools.py` 不依赖 LangGraph 是有意的——只有契约层能独立存在，
"哪些保证是框架给的、哪些是自己给的"才问得出来。改代码时请保持这条边界。

## 当前没有必做的续项

Dify 对照已从“安装完成但未初始化”推进到有证据的崩溃语义实验。若继续扩展，优先补已发布 API
（非 debugger SSE）与 broker visibility timeout 之后的长时间恢复，不要重复做相同的 UI 草稿测试。
当前数据库里 `crash-worker-001` 对应运行有意保持 `running`，用于保存这一次 worker 硬崩溃后的
悬挂状态证据；不要把它表述为 Dify 在所有部署与时间尺度下都会悬挂。

### 判断标准

不管补哪个框架，标准是同一条：**能不能说出一个该框架文档里写着、但用的人多半没意识到的语义。**
说得出来才算用过，说不出来就还是跑了个教程。

本仓库里那条是 `durability`——三档配置控制的是已完成步骤何时落盘，
管不到节点执行到一半崩掉，所以副作用幂等属于调用方责任。

## 结论的适用边界

写任何基于本仓库的材料前请一起说明：

- **这是为对齐语义写的对照实现，未上线、无真实用户。**
- 不要把结论说成"LangGraph 不行"。那是它文档写明的语义，说成缺陷是误读。
- 要一起说框架更强的那一块：`interrupt` + 检查点能让进程退出后再恢复审批，
  比自研那套同步阻塞干净。
- 只覆盖 StateGraph / checkpointer / interrupt 三块；
  LangChain 生态里的 RAG、向量库、Agent 预制件未使用过；Dify 只覆盖 1.16.1 的 HTTP Request、
  Human Input 与本地 debugger 草稿，Coze 等其他低代码平台仍是空白。
- 本仓库的数字（30×20、128/20/0、10 个 case）与 Guarded Desktop Agent 的数字
  （30×100、1420 项测试、13 个 case）是两套独立实验，不要混用。
