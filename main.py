import logging
import logging.handlers
import os
from typing import Any

from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .store import ForceSendStore, now_iso
from .state import ForceSendRuntime
from .cron_patch import CronForceSendPatch

logger = logging.getLogger(__name__)

PLUGIN_NAME = "astrbot_plugin_force_send"
SEND_TOOL_NAME = "send_message_to_user"
MAX_ATTEMPTS = 3
FILE_LOG_PATH = os.path.expanduser("~/force_send.log")


class ForceSendPlugin(Star):
    """AstrBot Force Send 插件。

    为 active_agent 定时任务增加强制发送能力。
    任务开启 force send 后，若一次执行中没有调用 send_message_to_user，
    最多重试 3 次。
    """

    def __init__(self, context: Context):
        super().__init__(context)

        # 计算配置存储路径
        plugin_data_dir = get_astrbot_plugin_data_path()
        config_dir = os.path.join(plugin_data_dir, PLUGIN_NAME)
        config_path = os.path.join(config_dir, "config.json")

        self.store = ForceSendStore(PLUGIN_NAME)
        self.store.set_config_path(config_path)

        self.runtime = ForceSendRuntime()
        self.patch = CronForceSendPatch(
            context=context,
            store=self.store,
            runtime=self.runtime,
            max_attempts=MAX_ATTEMPTS,
        )

        self._register_web_api(context)

    async def initialize(self):
        """插件初始化：加载配置、同步 cron job、安装 patch。"""
        logger.info("ForceSendPlugin initializing...")
        self._setup_debug_logging()
        await self.store.load()
        try:
            await self.store.sync_from_cron(self.context.cron_manager)
        except Exception as e:
            logger.warning(f"Initial sync from cron failed: {e}")
        self.patch.install()
        logger.info("ForceSendPlugin initialized")

    async def terminate(self):
        """插件卸载：卸载 patch、保存配置。"""
        logger.info("ForceSendPlugin terminating...")
        self.patch.uninstall()
        await self.store.save()
        logger.info("ForceSendPlugin terminated")

    # ========== Hooks ==========

    @filter.on_using_llm_tool()
    async def on_using_llm_tool(self, event, tool, tool_args):
        """检测 send_message_to_user 工具调用，标记 state.sent=True。"""
        tool_name = ""
        if hasattr(tool, "name"):
            tool_name = tool.name
        elif isinstance(tool, dict):
            tool_name = tool.get("name", "")
        else:
            tool_name = str(tool)

        if tool_name != SEND_TOOL_NAME:
            return

        job_id = self._extract_cron_job_id(event)
        if not job_id:
            return

        state = self.runtime.get_current(job_id)
        if state:
            state.sent = True
            logger.info(
                f"Detected {SEND_TOOL_NAME} for job {job_id}, "
                f"attempt {state.attempt}"
            )

    @filter.on_agent_done()
    async def on_agent_done(self, event, run_context, resp):
        """Agent 完成时兜底检查是否调用了 send_message_to_user。"""
        job_id = self._extract_cron_job_id(event)
        if not job_id:
            return

        state = self.runtime.get_current(job_id)
        if not state:
            return

        # 兜底检查 resp 中是否包含 send_message_to_user 调用
        if resp:
            tools_call_name = getattr(resp, "tools_call_name", None) or []
            if SEND_TOOL_NAME in tools_call_name:
                state.sent = True
                logger.info(
                    f"Agent done: detected {SEND_TOOL_NAME} via "
                    f"tools_call_name for job {job_id}"
                )

        state.last_resp = resp
        state.last_messages = list(
            getattr(run_context, "messages", []) or []
        )

    @staticmethod
    def _extract_cron_job_id(event) -> str | None:
        """从 event extras 中提取 cron job id。"""
        cron_job = None
        if hasattr(event, "get_extra"):
            try:
                cron_job = event.get_extra("cron_job")
            except Exception:
                pass
        if not cron_job:
            extras = getattr(event, "extras", {}) or {}
            cron_job = extras.get("cron_job")
        if isinstance(cron_job, dict):
            return cron_job.get("id")
        if hasattr(cron_job, "get"):
            return cron_job.get("id")
        return None

    # ========== Web API ==========

    async def api_list_jobs(self, request: Any):
        """GET /astrbot_plugin_force_send/jobs

        返回所有已同步的 active_agent 任务及其 force send 状态。
        """
        jobs_list = []
        for jid, jc in self.store.data.jobs.items():
            session = jc.session_snapshot
            can_force_send = bool(session.strip())
            jobs_list.append({
                "job_id": jid,
                "name": jc.name_snapshot,
                "description": jc.description_snapshot,
                "cron_expression": jc.cron_expression_snapshot,
                "enabled": jc.enabled_snapshot,
                "run_once": jc.run_once_snapshot,
                "next_run_time": jc.next_run_time_snapshot,
                "session": session,
                "force_send": jc.force_send,
                "can_force_send": can_force_send,
                "last_result": self.runtime.get_last_result(jid),
            })

        return {
            "last_sync_at": self.store.data.last_sync_at,
            "jobs": jobs_list,
        }

    async def api_set_force_send(self, request: Any):
        """POST /astrbot_plugin_force_send/jobs/<job_id>/force-send

        请求体: {"force_send": true}
        """
        job_id = request.path_params.get("job_id")
        if not job_id:
            return {"error": "Missing job_id"}

        try:
            body = await request.json()
        except Exception:
            return {"error": "Invalid JSON body"}

        force_send = body.get("force_send")
        if not isinstance(force_send, bool):
            return {"error": "force_send must be a boolean"}

        if job_id not in self.store.data.jobs:
            return {"error": f"Job {job_id} not found in force-send config"}

        self.store.set_force_send(job_id, force_send)
        await self.store.save()

        return {
            "success": True,
            "job_id": job_id,
            "force_send": force_send,
        }

    async def api_sync(self, request: Any):
        """POST /astrbot_plugin_force_send/sync

        手动触发同步，返回统计信息。
        """
        stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
        before_ids = set(self.store.data.jobs.keys())

        try:
            try:
                jobs = await self.context.cron_manager.list_jobs(
                    job_type="active_agent"
                )
            except TypeError:
                all_jobs = await self.context.cron_manager.list_jobs()
                jobs = [
                    j for j in all_jobs
                    if getattr(j, "job_type", None) == "active_agent"
                ]
        except Exception as e:
            return {"error": f"Failed to list cron jobs: {e}"}

        seen = set()
        for job in jobs:
            seen.add(job.job_id)
            existing = self.store.data.jobs.get(job.job_id)
            force_send = existing.force_send if existing else False
            self.store.data.jobs[job.job_id] = self.store._snapshot(
                job, force_send
            )
            if existing is None:
                stats["added"] += 1
            else:
                stats["updated"] += 1

        for job_id in list(self.store.data.jobs):
            if job_id not in seen:
                del self.store.data.jobs[job_id]
                stats["removed"] += 1

        self.store.data.last_sync_at = now_iso()
        await self.store.save()

        return {
            "success": True,
            "last_sync_at": self.store.data.last_sync_at,
            "stats": stats,
        }

    # ========== 路由注册 ==========

    def _register_web_api(self, context: Context):
        """注册三条 Web API 路由。"""
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

    @staticmethod
    def _setup_debug_logging():
        """添加文件日志输出到 ~/force_send.log（开发调试用）。"""
        plugin_logger = logging.getLogger("astrbot_plugin_force_send")
        log_path = FILE_LOG_PATH

        # 避免重复添加
        for handler in plugin_logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                try:
                    if handler.baseFilename == os.path.abspath(log_path):
                        return
                except (AttributeError, OSError):
                    pass

        try:
            handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=10 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            plugin_logger.addHandler(handler)
            plugin_logger.setLevel(logging.DEBUG)
            logger.info(f"Debug logging enabled: {log_path}")
        except Exception as e:
            logger.warning(f"Failed to setup file logging: {e}")
