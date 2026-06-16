import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ForceSendRunState:
    """单次 cron job 强制发送执行的运行状态。"""
    job_id: str
    attempt: int
    sent: bool = False
    last_resp: Any | None = None
    last_messages: list[Any] = field(default_factory=list)
    last_error: str | None = None
    started_at: float = field(default_factory=time.time)


class ForceSendRuntime:
    """管理所有 cron job 的强制发送运行时状态。"""

    def __init__(self):
        self._active: dict[str, ForceSendRunState] = {}
        self._last_results: dict[str, dict] = {}

    def begin(self, job_id: str, attempt: int) -> ForceSendRunState:
        """开始一次新的尝试，返回新的状态对象。"""
        state = ForceSendRunState(job_id=job_id, attempt=attempt)
        self._active[job_id] = state
        return state

    def end_current(self, job_id: str):
        """结束当前 job 的执行，从活跃状态中移除。"""
        self._active.pop(job_id, None)

    def get_current(self, job_id: str) -> Optional[ForceSendRunState]:
        """获取当前正在执行的 job 状态。"""
        return self._active.get(job_id)

    def record_result(
        self,
        job_id: str,
        *,
        success: bool = False,
        attempts: int = 0,
        skipped: bool = False,
        reason: str = "",
    ):
        """记录 job 的最终执行结果，供 WebUI 展示。"""
        finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._last_results[job_id] = {
            "success": success,
            "attempts": attempts,
            "skipped": skipped,
            "reason": reason,
            "finished_at": finished_at,
        }

    def get_last_result(self, job_id: str) -> Optional[dict]:
        """获取 job 最近一次执行结果。"""
        return self._last_results.get(job_id)
