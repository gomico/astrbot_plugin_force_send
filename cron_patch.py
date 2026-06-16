import asyncio
import copy
import logging
from typing import Any, Optional

from .state import ForceSendRuntime
from .store import ForceSendStore

logger = logging.getLogger(__name__)

SEND_TOOL_NAME = "send_message_to_user"


class CronForceSendPatch:
    """包装 CronJobManager._run_active_agent_job，实现强制发送重试。

    安装逻辑：
    - 检查是否已安装（_force_send_patched 标志）
    - 保存原始方法引用
    - 替换为 wrapped 方法

    执行逻辑：
    - 非 active_agent → 透传原始方法
    - force_send 关闭 → 透传原始方法
    - 缺少 session → 跳过重试，记录结果
    - 否则进入最多 max_attempts 次重试循环
    """

    def __init__(
        self,
        context: Any,
        store: ForceSendStore,
        runtime: ForceSendRuntime,
        max_attempts: int = 3,
    ):
        self.context = context
        self.store = store
        self.runtime = runtime
        self.max_attempts = max_attempts
        self.original = None
        self._job_locks: dict[str, asyncio.Lock] = {}

    def install(self):
        """安装 patch，替换 CronJobManager._run_active_agent_job。"""
        manager = self.context.cron_manager
        if getattr(manager, "_force_send_patched", False):
            logger.info("Force send patch already installed, skipping")
            return

        self.original = manager._run_active_agent_job

        async def wrapped(job, start_time):
            return await self._run_with_force_send(job, start_time)

        manager._run_active_agent_job = wrapped
        manager._force_send_patched = True
        logger.info("Force send patch installed")

    def uninstall(self):
        """卸载 patch，恢复原始方法。"""
        manager = self.context.cron_manager
        if self.original and getattr(manager, "_force_send_patched", False):
            manager._run_active_agent_job = self.original
            manager._force_send_patched = False
            logger.info("Force send patch uninstalled")

    async def _run_with_force_send(self, job, start_time):
        """判断是否需要强制发送，需要则进入重试循环。"""
        # 只处理 active_agent 类型
        if job.job_type != "active_agent":
            return await self.original(job, start_time)

        job_cfg = self.store.get_job(job.job_id)
        if not job_cfg or not job_cfg.force_send:
            return await self.original(job, start_time)

        # 检查是否有投递 session
        payload = job.payload or {}
        session = str(payload.get("session") or "").strip()
        if not session:
            logger.info(
                f"Job {job.job_id}: missing session, skipping force send"
            )
            self.runtime.record_result(
                job.job_id, skipped=True, reason="missing_session"
            )
            return await self.original(job, start_time)

        # 按 job 串行化
        lock = self._get_job_lock(job.job_id)
        async with lock:
            return await self._attempt_loop(job, start_time)

    def _get_job_lock(self, job_id: str) -> asyncio.Lock:
        """获取或创建 job 级别的锁，防止同一 job 并发执行。"""
        if job_id not in self._job_locks:
            self._job_locks[job_id] = asyncio.Lock()
        return self._job_locks[job_id]

    async def _attempt_loop(self, job, start_time):
        """最多 max_attempts 次重试循环。"""
        retry_context: Optional[dict] = None

        for attempt in range(1, self.max_attempts + 1):
            patched_job = self._build_attempt_job(job, attempt, retry_context)
            state = self.runtime.begin(job.job_id, attempt)

            logger.info(
                f"Force send attempt {attempt}/{self.max_attempts} "
                f"for job {job.job_id}"
            )

            try:
                await self.original(patched_job, start_time)
            except Exception as e:
                logger.error(
                    f"Force send attempt {attempt} failed for "
                    f"job {job.job_id}: {e}"
                )
                state.last_error = str(e)
            finally:
                self.runtime.end_current(job.job_id)

            if state.sent:
                logger.info(
                    f"Force send succeeded on attempt {attempt} "
                    f"for job {job.job_id}"
                )
                self.runtime.record_result(
                    job.job_id, success=True, attempts=attempt
                )
                return

            retry_context = self._build_retry_context(state)

        logger.warning(
            f"Force send failed after {self.max_attempts} attempts "
            f"for job {job.job_id}"
        )
        self.runtime.record_result(
            job.job_id, success=False, attempts=self.max_attempts
        )

    def _build_attempt_job(
        self, job, attempt: int, retry_context: Optional[dict]
    ):
        """构建某次尝试使用的 job 对象。

        第 1 次保持原 payload.note。
        第 2/3 次把 retry prompt 附加到 payload.note。
        """
        patched = copy.copy(job)
        orig_payload = job.payload or {}
        payload = dict(orig_payload)

        if attempt > 1 and retry_context:
            retry_prompt = self._build_retry_prompt(retry_context)
            existing_note = payload.get("note", "") or ""
            if existing_note:
                payload["note"] = existing_note + "\n\n" + retry_prompt
            else:
                payload["note"] = retry_prompt

        patched.payload = payload
        return patched

    def _build_retry_context(self, state) -> dict:
        """从执行状态构建重试上下文。"""
        return {
            "completion_text": _extract_completion_text(state.last_resp),
            "messages": state.last_messages,
            "last_error": state.last_error,
        }

    def _build_retry_prompt(self, ctx: dict) -> str:
        """构建 retry prompt，追加到 payload.note。"""
        completion_text = ctx.get("completion_text", "") or ""
        messages = ctx.get("messages", []) or []
        last_error = ctx.get("last_error", "") or ""

        header = (
            "[Force Send Retry]\n"
            "上一次定时任务执行没有调用 send_message_to_user。\n"
            "本任务已开启强制发送。不要重复执行已经完成的外部副作用操作。\n"
        )

        if completion_text:
            body = (
                "请调用 send_message_to_user，把下面内容发送给用户：\n\n"
                f"{completion_text}"
            )
        else:
            from .serializer import serialize_messages

            serialized = serialize_messages(messages)
            if serialized:
                body = (
                    "请根据下面的上一次执行过程，生成简洁汇总，"
                    "并调用 send_message_to_user 发送给用户。\n\n"
                    f"{serialized}"
                )
            else:
                body = (
                    "请生成一段简要说明，表示定时任务已完成，"
                    "并调用 send_message_to_user 发送给用户。"
                )

        if last_error:
            body += f"\n\n注意：上一次执行出现错误：{last_error}"

        return header + body


def _extract_completion_text(resp: Any) -> str:
    """从 agent 响应中提取最终文本。"""
    if resp is None:
        return ""
    if hasattr(resp, "completion_text"):
        return resp.completion_text or ""
    if hasattr(resp, "content"):
        return str(resp.content or "")
    return str(resp) if resp else ""
