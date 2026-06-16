import json
import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

TZ_CST = timezone(timedelta(hours=8), "Asia/Shanghai")


def now_iso() -> str:
    """返回当前时间的 ISO 格式字符串（CST / UTC+8）。"""
    return datetime.now(TZ_CST).isoformat()


@dataclass
class JobConfig:
    """单个 cron job 在 force send 插件中的配置。"""
    force_send: bool = False
    name_snapshot: str = ""
    description_snapshot: str = ""
    cron_expression_snapshot: str = ""
    enabled_snapshot: bool = True
    run_once_snapshot: bool = False
    next_run_time_snapshot: str = ""
    session_snapshot: str = ""
    updated_at: str = ""


@dataclass
class StoreData:
    """插件独立存储的数据模型。"""
    version: int = 1
    last_sync_at: str = ""
    jobs: dict[str, JobConfig] = field(default_factory=dict)


class ForceSendStore:
    """读写插件独立配置 JSON，提供同步逻辑。

    配置文件路径：
      <AstrBot data>/plugin_data/astrbot_plugin_force_send/config.json
    """

    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        self.data = StoreData()
        self._config_path: str | None = None

    def set_config_path(self, path: str):
        """设置配置文件完整路径。"""
        self._config_path = path

    async def load(self):
        """从磁盘加载配置，文件不存在时创建默认配置。"""
        path = self._config_path
        if not path:
            logger.warning("config_path not set, using default config")
            self.data = StoreData()
            return

        if not os.path.exists(path):
            logger.info(f"Config not found at {path}, creating default")
            self.data = StoreData()
            await self.save()
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.data = self._deserialize(raw)
            logger.debug(f"Loaded config with {len(self.data.jobs)} jobs")
        except (json.JSONDecodeError, KeyError, Exception) as e:
            logger.warning(f"Failed to load config, using default: {e}")
            self.data = StoreData()

    async def save(self):
        """原子写：先写 .tmp 文件，再 replace。"""
        path = self._config_path
        if not path:
            logger.warning("config_path not set, cannot save")
            return

        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._serialize(), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            raise

    # ---- 序列化 / 反序列化 ----

    def _serialize(self) -> dict:
        jobs = {}
        for jid, jc in self.data.jobs.items():
            jobs[jid] = {
                "force_send": jc.force_send,
                "name_snapshot": jc.name_snapshot,
                "description_snapshot": jc.description_snapshot,
                "cron_expression_snapshot": jc.cron_expression_snapshot,
                "enabled_snapshot": jc.enabled_snapshot,
                "run_once_snapshot": jc.run_once_snapshot,
                "next_run_time_snapshot": jc.next_run_time_snapshot,
                "session_snapshot": jc.session_snapshot,
                "updated_at": jc.updated_at,
            }
        return {
            "version": self.data.version,
            "last_sync_at": self.data.last_sync_at,
            "jobs": jobs,
        }

    def _deserialize(self, raw: dict) -> StoreData:
        data = StoreData(
            version=raw.get("version", 1),
            last_sync_at=raw.get("last_sync_at", ""),
        )
        for jid, jc_raw in raw.get("jobs", {}).items():
            data.jobs[jid] = JobConfig(
                force_send=jc_raw.get("force_send", False),
                name_snapshot=jc_raw.get("name_snapshot", ""),
                description_snapshot=jc_raw.get("description_snapshot", ""),
                cron_expression_snapshot=jc_raw.get("cron_expression_snapshot", ""),
                enabled_snapshot=jc_raw.get("enabled_snapshot", True),
                run_once_snapshot=jc_raw.get("run_once_snapshot", False),
                next_run_time_snapshot=jc_raw.get("next_run_time_snapshot", ""),
                session_snapshot=jc_raw.get("session_snapshot", ""),
                updated_at=jc_raw.get("updated_at", ""),
            )
        return data

    # ---- 配置访问 ----

    def get_job(self, job_id: str) -> Optional[JobConfig]:
        """获取指定 job 的 force send 配置。"""
        return self.data.jobs.get(job_id)

    def set_force_send(self, job_id: str, enabled: bool):
        """设置指定 job 的 force send 开关。"""
        if job_id in self.data.jobs:
            self.data.jobs[job_id].force_send = enabled
            self.data.jobs[job_id].updated_at = now_iso()

    # ---- 同步 ----

    async def sync_from_cron(self, cron_manager):
        """从 AstrBot CronJobManager 同步 active_agent 类型任务列表。

        只同步 job_type == 'active_agent' 的任务。
        新增 job 默认 force_send=false。
        已删除的 job 从配置中移除。
        """
        try:
            jobs = await cron_manager.list_jobs(job_type="active_agent")
        except TypeError:
            # 某些版本 list_jobs 不接受 type 参数
            all_jobs = await cron_manager.list_jobs()
            jobs = [j for j in all_jobs if getattr(j, "job_type", None) == "active_agent"]
        except Exception as e:
            logger.warning(f"sync_from_cron: list_jobs failed: {e}")
            return

        seen: set[str] = set()
        for job in jobs:
            seen.add(job.job_id)
            existing = self.data.jobs.get(job.job_id)
            force_send = existing.force_send if existing else False
            self.data.jobs[job.job_id] = self._snapshot(job, force_send)

        # 移除已删除的 job
        for job_id in list(self.data.jobs):
            if job_id not in seen:
                del self.data.jobs[job_id]

        self.data.last_sync_at = now_iso()
        await self.save()

        added = len(seen) - len(
            set(self.data.jobs.keys()) - seen
        )  # simplified: count new
        # 更准确的计数：新增数量 = 当前 total - (之前 total - 删除数量)
        logger.info(
            f"Sync complete: {len(self.data.jobs)} active_agent jobs tracked"
        )

    def _snapshot(self, job, force_send: bool) -> JobConfig:
        """从 CronJob 对象创建快照配置。"""
        payload = job.payload or {}
        session = str(payload.get("session") or "")
        return JobConfig(
            force_send=force_send,
            name_snapshot=job.name or "",
            description_snapshot=getattr(job, "description", "") or "",
            cron_expression_snapshot=job.cron_expression or "",
            enabled_snapshot=getattr(job, "enabled", True),
            run_once_snapshot=getattr(job, "run_once", False),
            next_run_time_snapshot=str(getattr(job, "next_run_time", "") or ""),
            session_snapshot=session,
            updated_at=now_iso(),
        )
