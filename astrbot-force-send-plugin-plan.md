# AstrBot 定时任务强制发送插件 — 设计备忘

## 需求概述

解决 AstrBot 定时任务触发时 LLM 可能不调用 `send_message_to_user` 工具的问题。

插件读取 AstrBot 的定时任务列表，给每个 `active_agent` 任务增加一个“强制发送”开关：

- 关：行为与不用插件相同
- 开：若 LLM 在一次任务执行过程中没有调用 `send_message_to_user`，插件要求 LLM 重试，最多重试 3 次，直到成功调用为止

成功判定按“只要本次任务执行过程中调用过一次 `send_message_to_user` 就算成功”。不要求工具调用一定出现在最终一步，也不检查平台侧消息是否真正投递成功。

## 源码关键路径

### 定时任务执行流程

`CronJobManager._run_job()` (`astrbot/core/cron/manager.py`)
  → `_run_active_agent_job()`
    → `_woke_main_agent()`
      → `build_main_agent()` 创建 agent runner
      → `runner.step_until_done(30)` 迭代执行
      → `runner.get_final_llm_resp()` 拿最终响应
      → `persist_agent_history()` 写入会话历史

### 关键数据结构

- **`LLMResponse`** — `tools_call_name`（`list[str]`）：LLM 响应中声明调用的工具名列表
- **`SendMessageToUserTool.name`** = `"send_message_to_user"`，定义在 `astrbot/core/tools/message_tools.py`
- **`CronJob.payload`**（`dict`）— AstrBot cron job 自身的自定义数据；本插件不写入该字段，只读取其中的 `session`、`note`、`sender_id` 等既有信息
- **`llm_resp.completion_text`** — LLM 最终输出的文本内容
- **`run_context.messages`** — Agent 执行过程中的消息历史，可作为重试上下文兜底来源

### Tool call 注入条件

`SendMessageToUserTool` 仅在 `delivery_session_str` 非空时才注入 `req.func_tool`。因此 force send 只对带投递会话的 `active_agent` cron job 有意义；没有 `payload.session` 的任务即使开启开关，也无法要求 LLM 调用该工具。

## 已明确的决策

1. 作用范围：仅限 AstrBot 自身的 `active_agent` 类型定时任务，与 Hermes cron 无关
2. 成功判定：一次任务执行过程中只要调用过一次 `send_message_to_user` 就判定成功
3. 重试上限：最多 3 次
4. 重试上下文：采用 B 方案
   - `completion_text` 非空：优先把上次最终文本作为“请发送给用户”的内容
   - `completion_text` 为空：传入 `run_context.messages` 的必要片段，并提示 LLM 基于完整执行过程生成汇总后调用 `send_message_to_user`
5. 配置存储：插件独立维护配置映射，不写 `CronJob.payload`
6. 配置同步：插件启动时同步 AstrBot cron job 列表，并在 WebUI 提供“立即同步”按钮
7. 用户配置：提供 WebUI 开关
8. 无需发送的场景：用户手动关闭该任务的 force send 开关即可

## 事件钩子结论

官方文档列出的相关事件钩子包括：

- `@filter.on_astrbot_loaded()`：Bot 初始化完成时触发
- `@filter.on_waiting_llm_request()`：准备调用 LLM、但尚未获取会话锁时触发
- `@filter.on_llm_request()`：调用 LLM 前触发，可修改 `ProviderRequest`
- `@filter.on_llm_response()`：LLM 请求完成后触发
- `@filter.on_agent_begin()`：Agent 开始运行时触发
- `@filter.on_using_llm_tool()`：Agent 准备调用 LLM 工具时触发
- `@filter.on_llm_tool_respond()`：LLM 工具调用完成后触发
- `@filter.on_agent_done()`：Agent 运行完成后触发
- `@filter.on_decorating_result()`：发送消息前触发
- `@filter.after_message_sent()`：发送消息后触发

文档说明 Agent/工具相关钩子适用于 AstrBot `> v4.23.1`。本地源码中 `MainAgentHooks` 会把 runner 的 `on_agent_begin`、`on_agent_done`、`on_tool_start`、`on_tool_end` 转成插件事件钩子；cron 路径创建的是 `CronMessageEvent` 并通过 `build_main_agent()` 运行 runner，因此可用这些 hook 做检测和上下文收集。

但 hook 本身只能观察或修改单次 LLM 请求，不能在 runner 完成后自动让 cron job 重跑。因此插件实现需要组合两层机制：

- 用 `on_using_llm_tool` 记录本次 cron job 是否调用过 `send_message_to_user`
- 用 `on_agent_done` 收集最终 `LLMResponse` 和 `run_context.messages`
- 用包装/patch `CronJobManager._run_active_agent_job` 或 `_woke_main_agent` 承担最多 3 次重试的控制流

## 推荐实现方案

### 1. 拦截点

优先包装 `CronJobManager._run_active_agent_job()`，而不是替换整个 manager：

- 保留 `_run_job()` 的状态更新、错误处理、一次性任务删除等行为
- 可以在进入 `_run_active_agent_job()` 时拿到完整 `CronJob`
- 对非 `active_agent` job 无影响

包装逻辑：

1. 判断 `job.job_type == "active_agent"`
2. 从插件配置映射读取该 job 是否开启 force send
3. 若未开启，调用原方法
4. 若开启但 `payload.session` 为空，记录 warning 后调用原方法或直接按失败处理（建议先调用原方法，避免插件改变原有执行）
5. 若开启且具备投递会话，进入最多 3 次执行循环
6. 每次执行前为当前 job 建立运行状态：`job_id`、`attempt`、`sent=False`、`last_resp=None`、`last_messages=None`
7. 调用原 `_run_active_agent_job()`
8. 执行结束后检查运行状态中的 `sent`
9. 成功则退出；失败则构造下一次重试提示并重跑

### 2. Tool call 检测

使用 `on_using_llm_tool` 做实时检测：

```python
@filter.on_using_llm_tool()
async def on_using_llm_tool(self, event, tool, tool_args):
    if tool.name == "send_message_to_user" and self._is_tracked_cron_event(event):
        self._mark_sent(event)
```

实时检测比只看最终 `LLMResponse.tools_call_name` 更稳，因为 LLM 可能中间调用了 `send_message_to_user`，最终一步只返回纯文本总结。

`on_agent_done` 作为兜底，可同时检查 `resp.tools_call_name`，并记录 `resp.completion_text` 与 `run_context.messages`：

```python
@filter.on_agent_done()
async def on_agent_done(self, event, run_context, resp):
    state = self._get_tracked_state(event)
    if not state:
        return
    if resp and "send_message_to_user" in (resp.tools_call_name or []):
        state.sent = True
    state.last_resp = resp
    state.last_messages = list(run_context.messages)
```

### 3. 重试上下文

采用 B 方案，尽量少传 token，同时处理 `completion_text` 为空的工具型任务。

第一次执行使用原 cron prompt。第 2/3 次执行时，在原始 note 基础上追加临时重试指令：

```text
[Force Send Retry]
上一次定时任务执行没有调用 send_message_to_user。
本任务已开启强制发送。请基于上一次执行结果，调用 send_message_to_user 发送给用户。

如果上一次最终文本非空，请优先发送以下内容：
{completion_text}

如果上一次最终文本为空，请根据上一次执行过程中的关键消息和工具结果，生成简洁汇总后发送。
```

上下文来源：

- `completion_text` 非空：只传最终文本
- `completion_text` 为空：传 `run_context.messages` 的必要片段
  - 保留最近 N 条消息，建议默认 8 条
  - 对工具结果做长度限制，避免巨大输出拖垮重试
  - 使用 `ProviderRequest.extra_user_content_parts` 或等价机制追加为临时上下文，避免污染长期 system prompt

如果包装 `_run_active_agent_job()` 无法直接修改 `_woke_main_agent()` 内部构造的 `ProviderRequest`，则先把重试提示拼进 `note/message`。后续若需要更干净的实现，再改为包装 `_woke_main_agent()` 并在构造 `ProviderRequest` 后注入 `extra_user_content_parts`。

### 4. 配置存储与同步

插件维护独立配置映射，例如：

```json
{
  "jobs": {
    "cron_job_id": {
      "force_send": true,
      "name_snapshot": "每日汇总",
      "session_snapshot": "aiocqhttp:GroupMessage:123456",
      "updated_at": "2026-06-16T12:00:00+08:00"
    }
  }
}
```

同步规则：

- 插件启动时读取 AstrBot cron job 列表
- 新增的 `active_agent` job 默认 `force_send=false`
- 已删除的 job 从配置映射中移除，或标记为 `orphaned` 后在 WebUI 隐藏（建议直接移除，保持简单）
- 非 `active_agent` job 不展示 force send 开关
- 每次同步刷新 `name_snapshot`、`description_snapshot`、`session_snapshot`，便于 WebUI 展示
- WebUI 提供“立即同步”按钮，手动触发同一套同步逻辑

配置文件建议放在插件自己的数据目录中，避免与 AstrBot cron job payload 耦合。

### 5. WebUI

WebUI 至少提供：

- 任务列表：名称、cron 表达式、启用状态、下次运行时间、投递 session、force send 开关
- 开关：逐个 job 开启/关闭 force send
- 立即同步按钮：从 AstrBot cron job 列表刷新插件映射
- 状态提示：显示最近同步时间、同步结果、被跳过的任务数量
- 运行提示：对没有 `payload.session` 的任务显示“无法强制发送：缺少投递会话”

可选增强：

- 最近一次 force send 运行结果：成功、重试次数、最后错误
- 批量开启/关闭
- 仅显示 `active_agent` 任务的过滤开关

## 边界与风险

- `send_message_to_user` 调用成功不等于平台最终投递成功；本设计明确只以调用工具作为成功标准
- 如果 AstrBot 版本低于 `v4.23.2`，Agent/工具相关 hook 可能不可用，需要降级到只看最终 `LLMResponse.tools_call_name`
- monkey patch/wrapper 需要在插件卸载时恢复原方法，避免热重载后重复包裹
- 多个 cron job 并发执行时，运行状态必须按 `job_id` 或事件 extras 隔离，不能用单一全局布尔值
- 重试可能造成任务副作用重复执行；B 方案在 `completion_text` 为空时传执行历史，但仍不能保证模型不会重复调用其他工具。后续实现时应在重试提示中明确“不要重复执行已完成的外部副作用操作，除非发送前必须补充信息”
- `run_context.messages` 可能包含较大的工具结果，必须做长度限制

## 可能的文件位置

插件目录：`~/AstrBotRepos/astrbot_plugin_force_send/`

根据已有惯例，AstrBot 插件 fork/clone 到 `~/AstrBotRepos/`，再 symlink 到 `~/AstrBot/data/plugins/`。

## 下一步实施清单

1. 搭建插件基本结构：`main.py`、配置模型、WebUI 页面
2. 实现 cron job 同步服务：读取 AstrBot cron job 列表并维护插件配置映射
3. 实现 `on_using_llm_tool` 与 `on_agent_done` 运行状态跟踪
4. 包装 `CronJobManager._run_active_agent_job()`，加入最多 3 次重试
5. 实现 WebUI 列表、force send 开关和立即同步按钮
6. 加测试或本地验证：普通任务不受影响、开启任务一次成功、开启任务重试成功、缺少 session 的任务提示正确
