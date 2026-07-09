"""
批量发布状态缓存服务

功能：
1. 维护批量发布任务的账号顺序和素材数量
2. 缓存每个账号自动获取商品的同步状态
3. 为批量发布状态接口提供内存快照查询
4. 支持批量发布任务取消/停止
"""
from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from typing import Any


class PublishBatchStatusService:
    """批量发布任务的内存状态缓存服务"""

    _lock = asyncio.Lock()
    _cache: dict[str, dict[str, Any]] = {}
    _ttl_seconds = 24 * 60 * 60

    @classmethod
    async def init_batch(
        cls,
        batch_id: str,
        account_ids: list[str],
        material_count: int,
        user_id: int | None = None,
    ) -> None:
        unique_account_ids = list(dict.fromkeys(account_ids))
        accounts = {
            account_id: {
                "account_id": account_id,
                "material_count": material_count,
                "sync_status": "pending",
                "sync_message": "等待该账号发布完成后自动获取商品",
                "sync_total_count": 0,
                "sync_saved_count": 0,
            }
            for account_id in unique_account_ids
        }
        async with cls._lock:
            cls._cleanup_locked()
            cls._cache[batch_id] = {
                "user_id": user_id,
                "account_order": unique_account_ids,
                "material_count": material_count,
                "accounts": accounts,
                "status": "running",
                "cancelled": False,
                "cancel_message": None,
                "cancelled_at": None,
                "last_access": time.time(),
            }

    @classmethod
    async def request_cancel(cls, batch_id: str, message: str = "用户已请求停止批量发布") -> bool:
        """请求取消批量发布任务。

        返回 True 表示找到任务并已标记取消；False 表示缓存中没有这个任务。
        后台执行逻辑会在账号/商品之间或发布执行中轮询这个标记并停止。
        """
        async with cls._lock:
            cls._cleanup_locked()
            record = cls._cache.get(batch_id)
            if not record:
                return False
            record["status"] = "cancelled"
            record["cancelled"] = True
            record["cancel_message"] = message
            record["cancelled_at"] = time.time()
            record["last_access"] = time.time()
            # 对尚未完成同步状态的账号，显示为已停止，避免前端一直显示等待中
            for account in (record.get("accounts") or {}).values():
                if account.get("sync_status") in {"pending", "running"}:
                    account["sync_status"] = "skipped"
                    account["sync_message"] = message
            return True

    @classmethod
    async def is_cancelled(cls, batch_id: str) -> bool:
        async with cls._lock:
            record = cls._cache.get(batch_id)
            if not record:
                return False
            record["last_access"] = time.time()
            return bool(record.get("cancelled"))

    @classmethod
    async def mark_account_sync_running(cls, batch_id: str, account_id: str) -> None:
        if await cls.is_cancelled(batch_id):
            await cls.mark_account_sync_skipped(batch_id, account_id, "批量发布已手动停止，未继续自动获取商品")
            return
        await cls._update_account(batch_id, account_id, sync_status="running", sync_message="正在自动获取该账号商品")

    @classmethod
    async def mark_account_sync_skipped(cls, batch_id: str, account_id: str, message: str) -> None:
        await cls._update_account(batch_id, account_id, sync_status="skipped", sync_message=message)

    @classmethod
    async def mark_account_sync_result(
        cls,
        batch_id: str,
        account_id: str,
        status: str,
        message: str,
        total_count: int = 0,
        saved_count: int = 0,
    ) -> None:
        await cls._update_account(
            batch_id,
            account_id,
            sync_status=status,
            sync_message=message,
            sync_total_count=total_count,
            sync_saved_count=saved_count,
        )

    @classmethod
    async def get_batch_snapshot(cls, batch_id: str) -> dict[str, Any] | None:
        async with cls._lock:
            cls._cleanup_locked()
            record = cls._cache.get(batch_id)
            if not record:
                return None
            record["last_access"] = time.time()
            return deepcopy(record)

    @classmethod
    async def clear_batch(cls, batch_id: str) -> None:
        async with cls._lock:
            cls._cache.pop(batch_id, None)

    @classmethod
    async def _update_account(cls, batch_id: str, account_id: str, **kwargs: Any) -> None:
        async with cls._lock:
            cls._cleanup_locked()
            record = cls._cache.get(batch_id)
            if not record:
                record = {
                    "user_id": None,
                    "account_order": [],
                    "material_count": 0,
                    "accounts": {},
                    "status": "running",
                    "cancelled": False,
                    "cancel_message": None,
                    "cancelled_at": None,
                    "last_access": time.time(),
                }
                cls._cache[batch_id] = record
            if account_id not in record["accounts"]:
                if account_id not in record["account_order"]:
                    record["account_order"].append(account_id)
                record["accounts"][account_id] = {
                    "account_id": account_id,
                    "material_count": record.get("material_count", 0),
                    "sync_status": "pending",
                    "sync_message": "等待该账号发布完成后自动获取商品",
                    "sync_total_count": 0,
                    "sync_saved_count": 0,
                }
            record["accounts"][account_id].update(kwargs)
            record["last_access"] = time.time()

    @classmethod
    def _cleanup_locked(cls) -> None:
        now = time.time()
        expired_batch_ids = [
            batch_id
            for batch_id, record in cls._cache.items()
            if now - float(record.get("last_access") or 0) > cls._ttl_seconds
        ]
        for batch_id in expired_batch_ids:
            cls._cache.pop(batch_id, None)
