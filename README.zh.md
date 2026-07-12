[English](./README.md) · [中文](./README.zh.md)

# inter-session

同一台机器上 Claude Code 会话之间的 agent-to-agent 消息传递。每个 Claude Code 会话连接到本地 WebSocket 总线，可以向其他已连接的会话发送消息；接收方会话把收到的消息当作 prompt，**默认按指令执行**。一个会话可以驱动另一个会话。

基于 Claude Code 的 `Monitor` 工具实现：毫秒级送达延迟，无主动轮询，没有消息时零 token 成本、零性能开销。不需要登陆 claude.ai 账号。无需任何配置。

仅本地(localhost)，目前仅支持 Unix(macOS、Linux、WSL2)。

![演示](./demo.svg)

## 与 subagent 和 agent team 有什么区别？

Claude Code 本身已有两种并发机制:[subagent](https://code.claude.com/docs/en/sub-agents)(`Agent` 工具 —— 在同一个会话内派生一个 worker 来处理某个聚焦的子任务)和 [agent team](https://code.claude.com/docs/en/agent-teams)(为某个任务一起启动的多个独立 CC 会话)。inter-session 走的是另一个维度:它连接**你已经打开的、长期运行的 Claude Code 会话** —— 分布在不同终端、不同项目里 —— 让它们彼此发消息。

| 维度       | Subagent                          | Agent team                                              | inter-session                                                          |
| :--------- | :-------------------------------- | :------------------------------------------------------ | :--------------------------------------------------------------------- |
| 上下文窗口 | 独立窗口；结果返回调用方           | 独立窗口；完全独立                                       | 独立窗口；完全独立；每个会话保留自己的用户对话历史                       |
| 通信方式   | 仅向主 agent 回报结果              | 队员之间直接互发消息                                     | 所有连接会话之间点对点通信                                              |
| 协调方式   | 主 agent 统筹所有工作              | 共享任务列表 + 自我协调                                  | 临时性 —— 每个会话各自应用自己的反应策略                                |
| 生命周期   | 按任务派生；任务完成即退出         | 由 lead 为单个任务派生                                   | 不派生 —— 连接的是你已打开的会话                                        |
| 由谁驱动   | 父 agent(程序化)                | Lead agent + 共享任务列表                                | 你 —— 每个会话都是你的；总线只负责让它们互发消息                        |
| 适用场景   | 只关心结果的聚焦子任务             | 单个任务内需要队员讨论与协作的复杂工作                   | 跨会话、跨长期独立工作的协调                                            |
| Token 成本 | 较低:结果概要返回主上下文         | 较高:每个队员都是独立的 Claude 实例                     | 仅在已运行的会话之上叠加每条消息的开销                                  |

**用 subagent**:当你需要一个快速、聚焦的 worker 返回一个总结。主对话保持干净。

**用 agent team**:当队员之间需要交换发现、相互质疑、独立协调时 —— 最适合存在竞争性假设的并行研究、并行代码审查，以及由不同队员负责不同部分的特性开发。

**用 inter-session**:当你有多个 Claude Code 会话正在为彼此独立的、长期的工作运行，而你希望其中一个驱动另一个 —— 比如把一个项目里会话发现的 bug 委派给另一个项目里的会话修复；让两个会话迭代多轮，每一轮各自积累的上下文都更加有价值；或者让两个已经积累了数小时对话历史的会话交换发现、彼此协调，而无需任何一方重启。每个会话保留自己的项目上下文、对话历史和工具权限；总线只负责在它们之间路由消息。

**过渡时机**:如果你发现自己在已打开的 Claude Code 会话之间反复复制粘贴，或者你的 agent team 任务跨越多个你正分别工作的项目，那么 inter-session 就是自然的下一步 —— 你已有的会话直接成为 team。

## 前置条件

- Python ≥ 3.10
- Claude Code ≥ 2.1.105

## 安装

在任意 Claude Code 会话中:

```
/plugin marketplace add https://github.com/yilunzhang/claude-code-inter-session
/plugin install inter-session
```

然后开始使用:

```
/inter-session:inter-session
```

Claude 会在第一次使用时自动处理运行时依赖安装 —— 无需额外设置。

monitor 默认**懒启动** —— 仅在你于某个 Claude Code 会话中第一次调用 `/inter-session:inter-session` 命令时启动。如果想改成每次会话打开时都自动连接，运行 `/inter-session:inter-session auto-start on`(然后 `/reload-plugins`)。

## 示例

示例 1 是简单的单轮模式。示例 2 和 3 展示**迭代循环** —— 多轮往返中每个会话的上下文随时间增值。这正是 subagent 和 agent team *做不好*的场景:subagent 每次调用都重置，agent team 任务结束就退出。

点击任意示例展开。

<details>
<summary><b>示例 1 — 跨项目 bug 修复</b> · 简单的单轮委派</summary>

两个 Claude Code 会话，各自处于不同项目。

**会话 A**(在 `~/proj/auth`):
```
/inter-session:inter-session
→ Connecting as `auth-refactor`…
```

**会话 B**(在 `~/proj/payments`):
```
/inter-session:inter-session
→ Connecting as `payments-debug`…
```

**会话 A**(用户提示):
```
send the bug you found to payments session and ask it to fix it.
```

**会话 B** 收到通知，修复 bug，然后回复:
```
[inter-session msg=q7r8 from="auth-refactor"] null deref in checkout.py:42 — user.email is unchecked; please add a guard and verify with the existing tests
→ Edits checkout.py to add the null guard
→ Runs pytest — 47 tests pass
→ Bash: send.py --to auth-refactor --text 'done: guarded user.email at checkout.py:42; 47 tests pass'
```

**会话 A** 看到:
```
[inter-session msg=k2m9 from="payments-debug"] done: guarded user.email at checkout.py:42; 47 tests pass
```

接收方 agent 在执行前会应用安全护栏(见 [Reaction policy](./skills/inter-session/SKILL.md))—— 破坏性操作需要消息中包含明确肯定的内容；含义不清的请求会先发 `question:` 询问。

</details>

<details>
<summary><b>示例 2 — implementer + reviewer</b> · TDD 风格的迭代循环</summary>

两个会话围绕一个复杂特性反复迭代。reviewer 积累的边界用例目录使每一轮都更加有的放矢 —— 上下文随每一轮*愈加有价值*。

**设置**:`impl` 会话在 `~/proj/rate-limiter` 写 token-bucket 实现；`reviewer` 会话在旁边充当对抗式测试作者。两个会话在循环过程中始终保持运行 —— 不为每一轮重新派生。

**第 1 轮**
```
impl     → "v1 pushed: basic per-key bucket with refill"
reviewer → reads code, writes 4 baseline tests, runs them
         → "3 pass, 1 fails: off-by-one at exactly-burst-threshold.
            asserts at tests/limiter/test_burst.py:42"
impl     → fixes → "v2"
```

**第 5 轮** —— reviewer 引用积累的目录:
```
reviewer → "v5 passes everything in test_burst.py and test_concurrent.py
            from rounds 1-4. New angle: clock skew. added test_clock_skew.py
            with 3 cases (NTP step, leap second, monotonic going backward).
            1 of 3 fails — leap second causes negative refill."
```
reviewer *知道*什么是已经稳固的(第 1–4 轮的用例)，所以可以去探索新领域，而不是重新检查基线。

**第 12 轮** —— 积累的上下文显式呈现:
```
reviewer → "all 18 prior cases still green. tried fuzzing with 100k random
            rate/burst combos: found 1 deterministic deadlock when burst=0.
            seed in tests/limiter/fuzz_seeds.json"
```
到第 12 轮，reviewer 已经拥有了自己的文件:`test_burst.py`、`test_concurrent.py`、`test_clock_skew.py`、`fuzz_seeds.json` —— 这是一个**不断增长的产物**，属于其会话状态的一部分。subagent 或新派生的队员每一轮都得从头重新发现这一切。

**结果**:约 30 轮，约 4 小时实际耗时，直到 reviewer 的对抗预算耗尽。`impl` 的会话保留了多次尝试的设计思路；`reviewer` 的会话保留了回归套件。你可以明天再回来继续 —— 两个会话都持久存在。

</details>

<details>
<summary><b>示例 3 — 红蓝对抗</b> · 对抗式安全循环，数百轮</summary>

两个会话长时间往返:一边是攻击目录，一边是补丁线索。两边都随每一轮增长。

**设置**:`red` 会话在 `~/proj/security-fuzz`，装备攻击脚本；`blue` 会话在 `~/proj/auth-service`，负责打补丁。到第 38 轮的时候，两边都已经积累了好几个小时的上下文。

**第 1 轮**
```
red  → "broke it: token reuse via cached redis lookup. repro:
        /tmp/red-001.sh; receives valid 401 token after rotation"
blue → "done: patched in PR-491 (drop-cache-on-rotate). retry."
```

**第 7 轮** —— red 引用补丁线索并转向新目标:
```
red  → "the cache fix from r1 holds. tried bypassing via header-smuggling
        (CRLF in X-Forwarded-For); session pinning bypassed in 2 of 4
        endpoints. catalog now: 7 working exploits, 6 patched."
blue → "patched header parser. retry."
```

**第 38 轮** —— 双方都掌握了「已加固区域」的地图:
```
red  → "no new bypass in this 30-min budget. attack catalog: 24 working
        at peak, all patched. notable patterns:
          cache-coherence       (rounds 1, 6, 19)
          header-smuggling      (rounds 2-5)
          session-state-confusion (rounds 12-17)"
blue → "tracked in PATCH_LOG.md (24 entries). all classes hardened.
        ready for external pentest."
```

到第 38 轮，两个会话都清楚已经尝试过什么、什么有效、什么无效。`red` 不会重试已知死路的攻击类别；`blue` 知道哪些表面已加固。积累的知识*就是*工作产出。subagent(每次调用上下文清零)和 agent team(任务结束就退出)都无法支撑这种多日的对抗式协作。

</details>

## 斜杠命令

| 命令                                                        | 作用                                                                                                |
| :---------------------------------------------------------- | :-------------------------------------------------------------------------------------------------- |
| `/inter-session:inter-session`                              | 连接(等同于 `connect`)。                                                                          |
| `/inter-session:inter-session connect [name]`               | 连接到总线；省略 `name` 时由 Claude 根据上下文提议。                                                |
| `/inter-session:inter-session list`                         | 列出已连接的会话。                                                                                  |
| `/inter-session:inter-session send <name> <text>`           | 向某一个会话发送消息。                                                                              |
| `/inter-session:inter-session broadcast <text>`             | 向所有其他会话广播(≤ 256 KB)。                                                                    |
| `/inter-session:inter-session rename <new-name>`            | 改名 —— 实现为断开 + 重连。                                                                         |
| `/inter-session:inter-session status`                       | 启发式连接状态。                                                                                    |
| `/inter-session:inter-session disconnect`                   | 停止 monitor。                                                                                      |
| `/inter-session:inter-session auto-start [on\|off\|status]` | 切换 auto-start。`on` = 每次会话启动时连接；`off` = 懒启动(默认)。改动后用 `/reload-plugins` 应用。 |

## 会话标签(label)

除了用于寻址的 `name`(ASCII 句柄)之外,会话还可以带一个可选的 **label** ——
一个简短的 Unicode 显示字符串(最多 60 个字符,例如 `Payments 🐛 refund bug`),
显示在 `list` 表格中。label 仅用于显示;寻址始终使用 `name`。

在 monitor 启动时通过客户端的 `--label` 参数设置。以这种方式设置的 label 会
**按项目记住** —— 持久化到数据目录,以 git 仓库根目录为键(不在仓库中时回退到
当前工作目录)—— 因此下次重启会自动复用,无需再次传入该参数:

- `--label "…"` —— 为当前项目设置并持久化。
- `--label ""` —— 清除已持久化的 label。
- `INTER_SESSION_LABEL` —— 一次性的运行时覆盖;会被使用但**不会**持久化。

## 插件配置

通过 `/plugin config` 可配置 WebSocket 端口与空闲关闭超时:

| 键                      | 类型   | 默认   | 说明                                                                  |
| :---------------------- | :----- | :----- | :-------------------------------------------------------------------- |
| `port`                  | number | `9473` | 总线监听的本地 WebSocket 端口。                                       |
| `idle_shutdown_minutes` | number | `10`   | 没有任何客户端连接时，服务器在多少分钟后退出。`0` 表示永不退出。       |

## 安全

- 服务器仅绑定 `127.0.0.1`。
- Bearer token 存放于 `~/.claude/data/inter-session/token`(权限 `0600`，目录 `0700`)。
- 任何以同一 Unix 用户身份运行的进程都可以读取该 token 并连接。对于单用户、单机场景这是可接受的。
- 该 token **不能**防御以你身份运行的恶意代码。如果你不信任本机代码，不要启用 inter-session。
- 接收方 agent 的反应策略(见 [SKILL.md](./skills/inter-session/SKILL.md))把对端消息当作指令处理，但与对待用户输入一样保持谨慎 —— 破坏性操作需要消息中包含明确肯定的内容；含义不清的请求会先发 `question:` 询问。

## 限制

- WebSocket 帧大小:16 MB。
- 直接消息 `text` 长度:10 MB。
- 广播 `text` 长度:256 KB。
- 标准输出通知正文:400 字符(Claude Code 在 ~512 字符处截断每条 monitor 通知，因此正文上限为前缀预留了空间)。超出后会截断为首行加一行指向 `~/.claude/data/inter-session/messages.log` 的 `cont` 指针,完整内容在该日志中始终保留。
- 广播频率:每个会话每分钟 60 次。

## 开发

全程 TDD。测试运行器:`pytest` + `pytest-asyncio`。本地开发完全在项目本地的 `.venv` 中运行，从不污染系统 Python。

```bash
make test         # 完整测试套件 —— 首次运行时自动 bootstrap .venv
make test-fast    # 跳过派生子进程的慢测试
make clean        # 删除 .venv
```

Makefile 优先使用 `uv`(若已安装)，否则回退到 `python3 -m venv`。

## 许可证

MIT —— 参见 [LICENSE](./LICENSE)。
