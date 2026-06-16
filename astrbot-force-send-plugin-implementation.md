# AstrBot Force Send 插件实现方案

## 目标

为 AstrBot 内置 `active_agent` 定时任务增加“强制发送”能力：

- 插件维护独立配置映射，不修改 AstrBot `CronJob.payload`
- WebUI 展示所有 `active_agent` 任务，并提供 force send 开关
- 插件启动时同步 cron job 列表，WebUI 提供“立即同步”
- 任务开启 force send 后，若一次执行过程中没有调用 `send_message_to_user`，最多重试 3 次
- 成功判定只看是否调用过一次 `send_message_to_user`，不检查平台最终投递结果

## 文件结构

```text
astrbot_plugin_force_send/
├─ main.py
├─ store.py
├─ cron_patch.py
├─ state.py
├─ serializer.py
├─ _conf_schema.json
└─ pages/
   └─ force-send/
      ├─ index.html
      ├─ app.js
      └─ style.css
```

职责划分：

- `main.py`：插件入口，注册 hook、Web API，安装/卸载 cron patch
- `store.py`：读写插件独立配置 JSON，提供同步逻辑
- `cron_patch.py`：包装 `CronJobManager._run_active_agent_job`
- `state.py`：维护单次 cron 执行的运行状态
- `serializer.py`：把 `run_context.messages` 压缩成重试提示可用的文本
- `pages/force-send/`：WebUI 页面

## 配置模型

配置文件放在插件数据目录，例如：

```text
<AstrBot data>/plugin_data/astrbot_plugin_force_send/config.json
```

数据结构：

```json
{
  "version": 1,
  "last_sync_at": "2026-06-16T12:00:00+08:00",
  "jobs": {
    "job-id": {
      "force_send": true,
      "name_snapshot": "每日汇总",
      "description_snapshot": "每天发送摘要",
      "cron_expression_snapshot": "0 9 * * *",
      "enabled_snapshot": true,
      "run_once_snapshot": false,
      "next_run_time_snapshot": "2026-06-17T09:00:00+08:00",
      "session_snapshot": "aiocqhttp:GroupMessage:123456",
      "updated_at": "2026-06-16T12:00:00+08:00"
    }
  }
}
```

实现约束：

- 新 job 默认 `force_send=false`
- 删除的 job 从 `jobs` 中移除
- 只同步 `job_type == "active_agent"` 的任务
- `payload.session` 为空的任务仍展示，但标记为不可强制发送
- 写文件使用原子写：先写 `.tmp`，再 `replace`

## 插件入口

`main.py` 中定义插件类：

```python
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_force_send"
SEND_TOOL_NAME = "send_message_to_user"
MAX_ATTEMPTS = 3


class ForceSendPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.store = ForceSendStore(PLUGIN_NAME)
        self.runtime = ForceSendRuntime()
        self.patch = CronForceSendPatch(
            context=context,
            store=self.store,
            runtime=self.runtime,
            max_attempts=MAX_ATTEMPTS,
        )
        self._register_web_api(context)

    async def initialize(self):
        await self.store.load()
        await self.store.sync_from_cron(self.context.cron_manager)
        self.patch.install()

    async def terminate(self):
        self.patch.uninstall()
        await self.store.save()
```

注意：

- `Context` 已暴露 `cron_manager`
- `initialize()` 中安装 patch，`terminate()` 中恢复原方法，避免插件重载后重复包裹
- `on_astrbot_loaded` 可作为兜底同步点，但核心初始化建议放在 `initialize()`

## 运行状态

`state.py` 定义一次执行状态：

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ForceSendRunState:
    run_id: str
    job_id: str
    attempt: int
    sent: bool = False
    last_resp: Any | None = None
    last_messages: list[Any] = field(default_factory=list)
    last_error: str | None = None
```

`ForceSendRuntime` 维护状态：

- `active_by_run_id: dict[str, ForceSendRunState]`
- `active_by_job_id: dict[str, ForceSendRunState]`
- `last_results: dict[str, dict]`，供 WebUI 展示最近运行状态

识别 cron event：

- `_run_active_agent_job()` 构造的 `extras["cron_job"]["id"]` 是 job id
- hook 中可从 `event.get_extra("cron_job")` 或事件 extras 读取
- patch 执行前创建 `run_id`，并把它写入重试时的 `job.payload` 或运行时状态

推荐最小实现先按 `job_id` 关联当前状态。若后续允许同一 job 并发执行，再改为向 `extras["cron_job"]` 注入 `force_send_run_id`，按 `run_id` 关联。

## Cron Patch

包装目标：

```python
CronJobManager._run_active_agent_job(self, job, start_time)
```

安装逻辑：

```python
class CronForceSendPatch:
    def install(self):
        manager = self.context.cron_manager
        if getattr(manager, "_force_send_patched", False):
            return
        self.original = manager._run_active_agent_job

        async def wrapped(job, start_time):
            return await self._run_with_force_send(job, start_time)

        manager._run_active_agent_job = wrapped
        manager._force_send_patched = True

    def uninstall(self):
        manager = self.context.cron_manager
        if self.original:
            manager._run_active_agent_job = self.original
            manager._force_send_patched = False
```

执行逻辑：

```python
async def _run_with_force_send(self, job, start_time):
    if job.job_type != "active_agent":
        return await self.original(job, start_time)

    job_cfg = self.store.get_job(job.job_id)
    if not job_cfg or not job_cfg.force_send:
        return await self.original(job, start_time)

    payload = job.payload or {}
    if not str(payload.get("session") or "").strip():
        self.runtime.record_result(job.job_id, skipped=True, reason="missing_session")
        return await self.original(job, start_time)

    retry_context = None
    for attempt in range(1, self.max_attempts + 1):
        patched_job = self._build_attempt_job(job, attempt, retry_context)
        state = self.runtime.begin(job.job_id, attempt)
        try:
            await self.original(patched_job, start_time)
        finally:
            self.runtime.end_current(job.job_id)

        if state.sent:
            self.runtime.record_result(job.job_id, success=True, attempts=attempt)
            return

        retry_context = self._build_retry_context(state)

    self.runtime.record_result(job.job_id, success=False, attempts=self.max_attempts)
```

`_build_attempt_job()` 不要直接修改原 `job`，使用浅拷贝或 `copy.copy(job)`：

- 第 1 次保持原 `payload.note`
- 第 2/3 次把 retry prompt 附加到 `payload.note`
- 保留原 `payload.session`，否则 `send_message_to_user` 不会注入

## Hook 实现

### 工具调用检测

```python
@filter.on_using_llm_tool()
async def on_using_llm_tool(self, event, tool, tool_args):
    if getattr(tool, "name", "") != SEND_TOOL_NAME:
        return

    job_id = self._extract_cron_job_id(event)
    if not job_id:
        return

    state = self.runtime.get_current(job_id)
    if state:
        state.sent = True
```

### Agent 完成兜底

```python
@filter.on_agent_done()
async def on_agent_done(self, event, run_context, resp):
    job_id = self._extract_cron_job_id(event)
    if not job_id:
        return

    state = self.runtime.get_current(job_id)
    if not state:
        return

    if resp and SEND_TOOL_NAME in (getattr(resp, "tools_call_name", None) or []):
        state.sent = True

    state.last_resp = resp
    state.last_messages = list(getattr(run_context, "messages", []) or [])
```

`_extract_cron_job_id()`：

```python
def _extract_cron_job_id(self, event):
    cron_job = None
    if hasattr(event, "get_extra"):
        cron_job = event.get_extra("cron_job")
    if not cron_job:
        cron_job = getattr(event, "extras", {}).get("cron_job")
    if isinstance(cron_job, dict):
        return cron_job.get("id")
    return None
```

## 重试提示

重试上下文采用 B 方案。

`completion_text` 非空：

```text
[Force Send Retry]
上一次定时任务执行没有调用 send_message_to_user。
本任务已开启强制发送。不要重复执行已经完成的外部副作用操作。
请调用 send_message_to_user，把下面内容发送给用户：

{completion_text}
```

`completion_text` 为空：

```text
[Force Send Retry]
上一次定时任务执行没有调用 send_message_to_user，且最终文本为空。
本任务已开启强制发送。不要重复执行已经完成的外部副作用操作。
请根据下面的上一次执行过程，生成简洁汇总，并调用 send_message_to_user 发送给用户。

{serialized_messages}
```

`serializer.py` 规则：

- 默认保留最近 8 条 `run_context.messages`
- 每条消息最多 1200 字符
- 总长度最多 6000 字符
- 优先保留 assistant 文本和 tool result 摘要
- 忽略图片、音频等无法稳定序列化的内容，替换为简短占位

## Web API

注册路由：

```python
def _register_web_api(self, context):
    context.register_web_api(
        f"/{PLUGIN_NAME}/jobs",
        self.api_list_jobs,
        ["GET"],
        "List force-send cron jobs",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/jobs/<job_id>/force-send",
        self.api_set_force_send,
        ["POST"],
        "Set force-send switch",
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/sync",
        self.api_sync,
        ["POST"],
        "Sync cron jobs",
    )
```

`GET /jobs` 返回：

```json
{
  "last_sync_at": "2026-06-16T12:00:00+08:00",
  "jobs": [
    {
      "job_id": "xxx",
      "name": "每日汇总",
      "description": "每天发送摘要",
      "cron_expression": "0 9 * * *",
      "enabled": true,
      "run_once": false,
      "next_run_time": "2026-06-17T09:00:00+08:00",
      "session": "aiocqhttp:GroupMessage:123456",
      "force_send": true,
      "can_force_send": true,
      "last_result": {
        "success": true,
        "attempts": 2,
        "finished_at": "2026-06-16T12:01:00+08:00"
      }
    }
  ]
}
```

`POST /jobs/<job_id>/force-send` 请求：

```json
{ "force_send": true }
```

校验：

- job 必须存在于插件配置映射
- `force_send` 必须是 bool
- 可允许给缺少 `session` 的任务开启，但返回 `can_force_send=false`；推荐前端禁用开关，后端仍做保护

`POST /sync`：

- 调用 `store.sync_from_cron(context.cron_manager)`
- 保存配置
- 返回同步统计：新增、更新、删除、跳过数量

## WebUI 页面

页面目录：

```text
pages/force-send/index.html
pages/force-send/app.js
pages/force-send/style.css
```

前端通过 `window.AstrBotPluginPage` bridge 调 API：

```js
const bridge = window.AstrBotPluginPage;
await bridge.ready();

const data = await bridge.apiGet("jobs");
await bridge.apiPost(`jobs/${jobId}/force-send`, { force_send: checked });
await bridge.apiPost("sync", {});
```

界面要素：

- 顶部状态栏：最近同步时间、任务数量、立即同步按钮
- 表格：任务名、cron、启用状态、下次运行、session、force send 开关、最近结果
- 缺少 session 时禁用开关并显示“缺少投递会话”
- API 请求期间禁用按钮，失败时展示错误提示

## 同步流程

`ForceSendStore.sync_from_cron()`：

```python
async def sync_from_cron(self, cron_manager):
    jobs = await cron_manager.list_jobs("active_agent")
    seen = set()

    for job in jobs:
        seen.add(job.job_id)
        existing = self.data.jobs.get(job.job_id)
        force_send = existing.force_send if existing else False
        self.data.jobs[job.job_id] = self._snapshot(job, force_send)

    for job_id in list(self.data.jobs):
        if job_id not in seen:
            del self.data.jobs[job_id]

    self.data.last_sync_at = now_iso()
    await self.save()
```

`_snapshot(job, force_send)` 从 `job.payload` 中提取：

- `session`
- `note`
- `sender_id`（仅调试展示，不建议默认显示）

## 并发与热重载

必须处理：

- 插件重载后不能重复 patch
- `terminate()` 必须恢复原 `_run_active_agent_job`
- 同一时间多个不同 cron job 可并发，状态按 `job_id` 分开
- 同一 job 理论上可能重入；第一版可以用 `asyncio.Lock` 按 job 串行化 force send 执行

建议：

```python
self._job_locks: dict[str, asyncio.Lock] = {}
```

在 `_run_with_force_send()` 内按 job id 加锁，避免同一 job 的 hook 状态互相覆盖。

## 日志

关键日志：

- 插件启动同步数量
- patch 安装/卸载
- job 开启 force send 后每次 attempt 开始/结束
- 检测到 `send_message_to_user`
- 缺少 session 跳过强制发送
- 3 次后仍失败

日志不要输出完整 `run_context.messages`，避免泄露上下文或刷屏。

## 验证清单

### 单元级

1. 配置文件不存在时能创建默认配置
2. 同步新增 `active_agent` job，默认 `force_send=false`
3. 同步删除已不存在 job
4. `serializer` 能限制单条和总长度
5. hook 能从 cron event extras 中提取 job id

### 集成级

1. force send 关闭：只执行原 `_run_active_agent_job()` 一次
2. force send 开启且第一次调用 `send_message_to_user`：不重试
3. force send 开启且第一次没调用、第二次调用：执行 2 次后成功
4. force send 开启且 3 次都没调用：记录失败，不再继续
5. 缺少 `payload.session`：不进入重试，WebUI 显示不可强制发送
6. 插件重载：patch 被恢复并重新安装一次，不出现重复重试

### 人工验证

1. 在 AstrBot WebUI 创建一个 `active_agent` 定时任务
2. 打开插件 Page，点击“立即同步”
3. 开启该任务的 force send
4. 点击 AstrBot 定时任务的立即运行
5. 观察日志中 attempt 与工具调用记录
6. 确认任务调用 `send_message_to_user` 后停止重试

## 实施顺序

1. 写 `store.py` 和配置同步，先让 Web API 能列出任务
2. 写 `pages/force-send`，完成列表、开关、立即同步
3. 写 `state.py` 和两个 hook，确认能检测 `send_message_to_user`
4. 写 `cron_patch.py`，先实现 force send 关闭时完全透传
5. 加入最多 3 次重试和 retry prompt
6. 加 serializer 限长逻辑
7. 做插件热重载、缺少 session、连续失败的边界验证
