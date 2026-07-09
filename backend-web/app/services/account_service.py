"""
账号服务

功能：
1. 账号CRUD操作
2. 账号状态管理
3. Cookie更新
4. 扫码登录账号创建/更新
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update, text
from sqlalchemy.ext.asyncio import AsyncSession

from common.services.account_limit_service import AccountLimitService
from common.models.xy_account import XYAccount
from common.utils.cookie_refresh import clear_cookie_refresh_snapshot

# UTC时区常量
UTC = timezone.utc


def _normalize_account_category(category: str | None) -> str:
    """归一化账号分类，避免写入空字符串。"""
    normalized = (category or "默认").strip()
    return normalized or "默认"


def _get_offline_supported_from_metadata(metadata: dict | None) -> bool:
    """读取账号下架权限标记。

    默认 True：避免升级后误拦截历史上能正常下架的鱼小铺账号。
    一旦闲鱼返回无权限，后端会自动写入 False。
    """
    if not isinstance(metadata, dict):
        return True
    value = metadata.get("offline_supported")
    if value is None:
        return True
    return bool(value)


class AccountService:
    """Provides access to legacy cookie account records."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _next_sort_order(self, category: str, owner_id: int | None = None) -> int:
        """获取指定分组下一个排序值，用于移动到分组末尾。"""
        normalized_category = _normalize_account_category(category)
        stmt = select(func.coalesce(func.max(XYAccount.sort_order), 0)).where(
            XYAccount.category == normalized_category
        )
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0) + 1

    async def _top_sort_order(self, category: str, owner_id: int | None = None) -> int:
        """获取指定分组顶部排序值，用于新账号默认置顶。

        账号列表按 sort_order 升序展示，因此新账号取当前最小值 - 1，
        就能稳定排到该分组顶部，不影响用户已保存的账号顺序。
        """
        normalized_category = _normalize_account_category(category)
        stmt = select(func.coalesce(func.min(XYAccount.sort_order), 0)).where(
            XYAccount.category == normalized_category
        )
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0) - 1

    async def list_categories(self, owner_id: int | None = None) -> list[str]:
        """返回账号分组列表。

        v1.0.2.1：优先读取持久化分组表 xy_account_groups，同时兼容账号表里已有的 category。
        这样即使新分组下面暂时没有账号，刷新页面后分组也不会消失。
        """
        categories: list[str] = []

        def append_category(value: str | None) -> None:
            normalized = _normalize_account_category(value)
            if normalized not in categories:
                categories.append(normalized)

        # 只固定保留“默认”。其他分组从数据库读取，可新增也可删除。
        append_category("默认")

        # 读取持久化分组表。兼容未执行 SQL 的情况：失败时不影响账号列表。
        try:
            if owner_id is None:
                group_sql = text("""
                    SELECT DISTINCT name
                    FROM xy_account_groups
                    WHERE name IS NOT NULL AND name <> ''
                    ORDER BY sort_order ASC, name ASC
                """)
                group_result = await self.session.execute(group_sql)
            else:
                group_sql = text("""
                    SELECT DISTINCT name
                    FROM xy_account_groups
                    WHERE owner_id = :owner_id AND name IS NOT NULL AND name <> ''
                    ORDER BY sort_order ASC, name ASC
                """)
                group_result = await self.session.execute(group_sql, {"owner_id": owner_id})
            for row in group_result.fetchall():
                append_category(row[0])
        except Exception:
            pass

        # 兼容历史数据：账号表里已存在的 category 也要显示。
        stmt = select(XYAccount.category).distinct().order_by(XYAccount.category)
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        for item in result.scalars().all():
            append_category(item)

        return categories

    async def create_category(self, category: str, owner_id: int) -> str:
        """创建持久化账号分组。已存在时直接复用。"""
        normalized = _normalize_account_category(category)
        # MySQL 8 / MariaDB 均支持 ON DUPLICATE KEY UPDATE
        sql = text("""
            INSERT INTO xy_account_groups (owner_id, name, sort_order)
            VALUES (
                :owner_id,
                :name,
                COALESCE((
                    SELECT max_sort FROM (
                        SELECT MAX(sort_order) AS max_sort
                        FROM xy_account_groups
                        WHERE owner_id = :owner_id_for_sort
                    ) AS t
                ), 0) + 1
            )
            ON DUPLICATE KEY UPDATE name = VALUES(name)
        """)
        await self.session.execute(sql, {
            "owner_id": owner_id,
            "owner_id_for_sort": owner_id,
            "name": normalized,
        })
        await self.session.commit()
        return normalized


    async def delete_category(self, category: str, owner_id: int) -> dict:
        """删除账号分组。

        为避免账号丢失，删除分组时会把该分组下的账号移动到“默认”。
        “默认”分组不可删除。
        """
        normalized = _normalize_account_category(category)
        if normalized == "默认":
            raise ValueError("默认分组不能删除")

        # 确保默认分组存在
        await self.create_category("默认", owner_id)

        update_stmt = (
            update(XYAccount)
            .where(XYAccount.category == normalized)
            .values(category="默认", sort_order=0)
        )
        if owner_id is not None:
            update_stmt = update_stmt.where(XYAccount.owner_id == owner_id)
        update_result = await self.session.execute(update_stmt)

        delete_sql = text("""
            DELETE FROM xy_account_groups
            WHERE owner_id = :owner_id AND name = :name
        """)
        delete_result = await self.session.execute(delete_sql, {"owner_id": owner_id, "name": normalized})
        await self.session.commit()

        return {
            "category": normalized,
            "moved_count": int(update_result.rowcount or 0),
            "deleted_count": int(delete_result.rowcount or 0),
        }

    async def ensure_category(self, category: str, owner_id: int | None = None) -> str:
        """确保分组名称存在；owner_id 为空时仅做归一化。"""
        normalized = _normalize_account_category(category)
        if owner_id is not None:
            try:
                await self.create_category(normalized, owner_id)
            except Exception:
                # 分组表异常不影响账号主流程
                await self.session.rollback()
        return normalized

    async def list_account_options(self, owner_id: int | None = None) -> list[dict]:
        stmt = select(
            XYAccount.id,
            XYAccount.account_id,
            XYAccount.remark,
            XYAccount.status,
            XYAccount.show_browser,
            XYAccount.metadata_json,
            XYAccount.category,
            XYAccount.sort_order,
        ).order_by(XYAccount.category, XYAccount.sort_order, XYAccount.account_id)
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        return [
            {
                "pk": row.id,
                "id": row.account_id,
                "remark": row.remark or "",
                "enabled": (row.status or "active").strip().lower() not in {"inactive", "disabled", "suspended", "deleted"},
                "show_browser": bool(row.show_browser),
                "offline_supported": _get_offline_supported_from_metadata(row.metadata_json),
                "category": row.category or "默认",
                "sort_order": row.sort_order or 0,
            }
            for row in result.all()
        ]

    async def list_account_ids(self, owner_id: int | None = None) -> list[str]:
        """获取账号ID列表，owner_id为None时返回所有账号（管理员）"""
        stmt = select(XYAccount.account_id).order_by(XYAccount.category, XYAccount.sort_order, XYAccount.account_id)
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_accounts(self, owner_id: int | None = None) -> list[XYAccount]:
        """获取账号列表，owner_id为None时返回所有账号（管理员）"""
        stmt = select(XYAccount).order_by(XYAccount.category, XYAccount.sort_order, XYAccount.account_id)
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_accounts_paginated(
        self,
        owner_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        category: str | None = None,
        ai_reply: bool | None = None,
        scheduled_redelivery: bool | None = None,
        scheduled_rate: bool | None = None,
        auto_polish: bool | None = None,
        auto_confirm: bool | None = None,
        has_password: bool | None = None,
        disable_reason: str | None = None,
        account_id: str | None = None,
        online: bool | None = None,
        online_account_ids: list[str] | None = None,
    ) -> tuple[list[XYAccount], int]:
        """获取账号列表（分页），支持多条件筛选
        
        Args:
            owner_id: 用户ID，None表示查询所有用户（管理员）
            page: 页码
            page_size: 每页数量
            status: 状态筛选（active/inactive）
            category: 账号分类筛选
            ai_reply: AI回复开关筛选
            scheduled_redelivery: 定时补发货筛选
            scheduled_rate: 定时补评价筛选
            auto_polish: 商品擦亮筛选
            auto_confirm: 自动确认收货筛选
            has_password: 是否配置密码筛选
            disable_reason: 禁用原因模糊搜索关键词（LIKE %keyword%）
            account_id: 账号ID模糊搜索关键词（LIKE %keyword%）
            online: 在线状态筛选（True=仅在线 / False=仅离线 / None=不筛选）
            online_account_ids: 当前在线账号ID集合（口径同仪表盘“在线账号”，由调用方实时取得）

        Returns:
            (账号列表, 总数)
        """
        from sqlalchemy import func, and_, or_
        
        base_stmt = select(XYAccount)
        conditions = []
        
        # 用户ID筛选
        if owner_id is not None:
            conditions.append(XYAccount.owner_id == owner_id)
        
        # 状态筛选 - 与 _status_to_enabled 函数保持一致
        # inactive/disabled/suspended/deleted 视为禁用，其他视为启用
        if status is not None:
            inactive_statuses = ["inactive", "disabled", "suspended", "deleted"]
            if status == "active":
                # 启用：status 不在禁用列表中
                conditions.append(~XYAccount.status.in_(inactive_statuses))
            elif status == "inactive":
                # 禁用：status 在禁用列表中
                conditions.append(XYAccount.status.in_(inactive_statuses))
        

        # 账号分类筛选
        if category is not None:
            category_keyword = category.strip()
            if category_keyword:
                conditions.append(XYAccount.category == category_keyword)

        # AI回复筛选（从metadata_json中获取）
        if ai_reply is not None:
            if ai_reply:
                # AI回复开启：兼容 ai_enabled 与历史 enabled 字段
                conditions.append(
                    or_(
                        XYAccount.metadata_json["ai_reply_settings"]["ai_enabled"].as_boolean() == True,
                        XYAccount.metadata_json["ai_reply_settings"]["enabled"].as_boolean() == True,
                    )
                )
            else:
                # AI回复关闭：metadata_json为空，或 ai_enabled/enabled 都未开启
                conditions.append(
                    or_(
                        XYAccount.metadata_json.is_(None),
                        XYAccount.metadata_json["ai_reply_settings"]["ai_enabled"].as_boolean() == False,
                        XYAccount.metadata_json["ai_reply_settings"]["enabled"].as_boolean() == False,
                        and_(
                            XYAccount.metadata_json["ai_reply_settings"]["ai_enabled"].is_(None),
                            XYAccount.metadata_json["ai_reply_settings"]["enabled"].is_(None),
                        )
                    )
                )
        
        # 定时补发货筛选
        if scheduled_redelivery is not None:
            conditions.append(XYAccount.scheduled_redelivery == scheduled_redelivery)
        
        # 定时补评价筛选
        if scheduled_rate is not None:
            conditions.append(XYAccount.scheduled_rate == scheduled_rate)
        
        # 商品擦亮筛选
        if auto_polish is not None:
            conditions.append(XYAccount.auto_polish == auto_polish)
        
        # 自动确认收货筛选
        if auto_confirm is not None:
            conditions.append(XYAccount.auto_confirm == auto_confirm)
        
        # 禁用原因模糊搜索（忽略空白字符串；ilike 大小写不敏感，自动参数化避免 SQL 注入；与项目其它筛选保持风格一致）
        if disable_reason is not None:
            keyword = disable_reason.strip()
            if keyword:
                conditions.append(XYAccount.disable_reason.ilike(f"%{keyword}%"))
        
        # 账号ID模糊搜索（忽略空白字符串；ilike 自动参数化避免 SQL 注入；与禁用原因模糊搜索保持风格一致）
        if account_id is not None:
            account_id_keyword = account_id.strip()
            if account_id_keyword:
                conditions.append(XYAccount.account_id.ilike(f"%{account_id_keyword}%"))

        # 在线状态筛选：在线集合来自 websocket 实时连接（不在库内），
        # 故以 account_id IN / NOT IN 在线集合 的方式参与 SQL 条件，保证分页正确。
        # 空集合时：online=True 匹配为空（无人在线）；online=False 匹配全部（与语义一致）。
        if online is not None:
            online_ids = [str(x) for x in (online_account_ids or [])]
            if online:
                conditions.append(XYAccount.account_id.in_(online_ids))
            else:
                conditions.append(XYAccount.account_id.notin_(online_ids))
        
        # 是否配置密码筛选（账号和密码都配置了才算已配置）
        if has_password is not None:
            if has_password:
                # 已配置：username和login_password都不为空
                conditions.append(
                    and_(
                        XYAccount.username.isnot(None),
                        XYAccount.username != '',
                        XYAccount.login_password.isnot(None),
                        XYAccount.login_password != ''
                    )
                )
            else:
                # 未配置：username或login_password为空
                conditions.append(
                    or_(
                        XYAccount.username.is_(None),
                        XYAccount.username == '',
                        XYAccount.login_password.is_(None),
                        XYAccount.login_password == ''
                    )
                )
        
        # 应用所有条件
        if conditions:
            base_stmt = base_stmt.where(and_(*conditions))
        
        # 查询总数：直接基于条件统计，避免把整表 SELECT 包进子查询
        count_stmt = select(func.count(XYAccount.id))
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar() or 0
        
        # 分页查询：启用账号排在前面，再按分组与自定义排序展示
        from sqlalchemy import case
        inactive_statuses_list = ["inactive", "disabled", "suspended", "deleted"]
        status_order = case(
            (XYAccount.status.in_(inactive_statuses_list), 1),
            else_=0
        )
        offset = (page - 1) * page_size
        stmt = base_stmt.order_by(
            status_order,
            XYAccount.category,
            XYAccount.sort_order,
            XYAccount.created_at.desc(),
            XYAccount.id.desc(),
        ).offset(offset).limit(page_size)
        result = await self.session.execute(stmt)
        
        return list(result.scalars().all()), total

    async def list_all_accounts(self) -> list[XYAccount]:
        """获取所有账号（用于启动时加载）"""
        stmt = select(XYAccount).order_by(XYAccount.category, XYAccount.sort_order, XYAccount.account_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_enabled_accounts(self) -> list[XYAccount]:
        """获取所有启用的账号
        
        Returns:
            启用状态的账号列表
        """
        stmt = (
            select(XYAccount)
            .where(XYAccount.status == "active")
            .order_by(XYAccount.category, XYAccount.sort_order, XYAccount.account_id)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_account_for_user(self, owner_id: int | None, account_identifier: str) -> XYAccount | None:
        """
        获取指定用户的账号
        
        Args:
            owner_id: 用户ID，如果为 None 则不限制用户（管理员模式）
            account_identifier: 账号标识（支持 account_id 或 unb）
            
        Returns:
            账号对象，如果不存在则返回 None
        """
        # 先按 account_id 精确查询（account_id 全局唯一，走 uk_account_id 索引），避免 OR 导致索引失效
        stmt = select(XYAccount).where(XYAccount.account_id == account_identifier)
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        account = result.scalars().first()

        # account_id 未命中时，再按 unb 查询（兼容历史数据）
        if account is None:
            stmt2 = select(XYAccount).where(XYAccount.unb == account_identifier)
            if owner_id is not None:
                stmt2 = stmt2.where(XYAccount.owner_id == owner_id)
            result2 = await self.session.execute(stmt2)
            account = result2.scalars().first()

        return account

    async def get_accounts_for_user(self, owner_id: int | None, account_ids: list[str]) -> list[XYAccount]:
        if not account_ids:
            return []
        stmt = select(XYAccount).where(XYAccount.account_id.in_(account_ids))
        if owner_id is not None:
            stmt = stmt.where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_account_by_identifier(self, account_identifier: str) -> XYAccount | None:
        """根据账号标识获取账号（不限制用户，管理员使用）"""
        stmt = select(XYAccount).where(XYAccount.account_id == account_identifier)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def account_id_exists(self, account_id: str, exclude_pk: int | None = None) -> bool:
        """检查 account_id 是否已存在（全局，不区分所属用户）

        Args:
            account_id: 待校验的账号ID
            exclude_pk: 需排除的账号主键（用于更新场景，排除自身）

        Returns:
            True 表示已存在，False 表示不存在
        """
        stmt = select(func.count(XYAccount.id)).where(XYAccount.account_id == account_id)
        if exclude_pk is not None:
            stmt = stmt.where(XYAccount.id != exclude_pk)
        result = await self.session.execute(stmt)
        return (result.scalar() or 0) > 0

    async def create_account(
        self,
        owner_id: int,
        account_id: str,
        cookie_value: str,
        *,
        unb: str | None = None,
        login_method: str = "manual",
        category: str | None = None,
    ) -> XYAccount:
        # 全局唯一校验：account_id 不允许与任何用户的账号重复
        if await self.account_id_exists(account_id):
            raise ValueError("账号ID已存在")

        await AccountLimitService(self.session).ensure_can_add_account(owner_id)

        normalized_category = await self.ensure_category(category, owner_id)
        account = XYAccount(
            owner_id=owner_id,
            account_id=account_id,
            cookie=cookie_value,
            login_method=login_method,
            status="active",
            category=normalized_category,
            sort_order=await self._top_sort_order(normalized_category, owner_id),
            auto_confirm=False,
            pause_duration=10,
            show_browser=False,
            unb=unb,
            last_login_at=datetime.now(tz=UTC),
        )
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        return account

    async def update_cookie(self, account: XYAccount, value: str) -> None:
        account.cookie = value
        account.metadata_json = clear_cookie_refresh_snapshot(account.metadata_json)
        self.session.add(account)
        await self.session.commit()

    async def update_status(self, account: XYAccount, enabled: bool, disable_reason: str | None = None) -> None:
        """更新账号状态
        
        Args:
            account: 账号对象
            enabled: 是否启用
            disable_reason: 禁用原因（仅在禁用时有效，启用时会清空）
        """
        account.status = "active" if enabled else "disabled"
        # 启用时清空禁用原因，禁用时设置禁用原因
        account.disable_reason = None if enabled else disable_reason
        self.session.add(account)
        await self.session.commit()

    async def update_remark(self, account: XYAccount, remark: str) -> None:
        account.remark = remark
        self.session.add(account)
        await self.session.commit()

    async def update_offline_supported(self, account: XYAccount, offline_supported: bool) -> None:
        """更新账号是否支持接口下架/鱼小铺权限标记，存入 metadata，避免新增数据库字段。"""
        metadata = dict(account.metadata_json or {})
        metadata["offline_supported"] = bool(offline_supported)
        account.metadata_json = metadata
        self.session.add(account)
        await self.session.commit()

    async def update_account_id(self, account: XYAccount, new_account_id: str) -> str:
        """更新账号ID，并同步所有以 account_id 字符串引用该账号的业务表。"""
        normalized_new_id = (new_account_id or "").strip()
        if not normalized_new_id:
            raise ValueError("账号ID不能为空")
        if len(normalized_new_id) > 80:
            raise ValueError("账号ID不能超过80个字符")

        old_account_id = account.account_id
        if normalized_new_id == old_account_id:
            return normalized_new_id

        if await self.account_id_exists(normalized_new_id, exclude_pk=account.id):
            raise ValueError("新的账号ID已存在")

        # 先更新主账号表。xy_accounts.account_id 有唯一索引，重复会被数据库拦截。
        await self.session.execute(
            update(XYAccount)
            .where(XYAccount.id == account.id)
            .values(account_id=normalized_new_id)
        )

        # 兼容项目中大量按 account_id 字符串保存关联关系的表。
        # 只更新当前数据库中字段名为 account_id 的字符列，避免误改数值型主键。
        columns_sql = text("""
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND column_name = 'account_id'
              AND table_name <> 'xy_accounts'
              AND data_type IN ('varchar', 'char', 'text', 'tinytext', 'mediumtext', 'longtext')
        """)
        result = await self.session.execute(columns_sql)
        table_names = [row[0] for row in result.fetchall()]

        for table_name in table_names:
            safe_table_name = str(table_name).replace('`', '``')
            await self.session.execute(
                text(f"UPDATE `{safe_table_name}` SET account_id = :new_id WHERE account_id = :old_id"),
                {"new_id": normalized_new_id, "old_id": old_account_id},
            )

        await self.session.commit()
        account.account_id = normalized_new_id
        return normalized_new_id

    async def update_category(self, account: XYAccount, category: str | None) -> None:
        """更新账号分类/分组；切换分组时放到新分组末尾。"""
        normalized_category = _normalize_account_category(category)
        await self.ensure_category(normalized_category, account.owner_id)
        values = {"category": normalized_category}
        if normalized_category != (account.category or "默认"):
            values["sort_order"] = await self._next_sort_order(normalized_category, account.owner_id)
        stmt = (
            update(XYAccount)
            .where(XYAccount.id == account.id)
            .values(**values)
        )
        await self.session.execute(stmt)
        await self.session.commit()
        account.category = normalized_category
        if "sort_order" in values:
            account.sort_order = int(values["sort_order"])

    async def update_accounts_category(self, account_ids: list[str], category: str, owner_id: int | None = None) -> int:
        """批量移动账号到指定分组。"""
        normalized_ids = list(dict.fromkeys(account_id.strip() for account_id in account_ids if account_id and account_id.strip()))
        if not normalized_ids:
            return 0

        normalized_category = _normalize_account_category(category)
        if owner_id is not None:
            await self.ensure_category(normalized_category, owner_id)

        accounts = await self.get_accounts_for_user(owner_id, normalized_ids)
        next_order = await self._next_sort_order(normalized_category, owner_id)
        for account in accounts:
            account.category = normalized_category
            account.sort_order = next_order
            next_order += 1
            self.session.add(account)

        await self.session.commit()
        return len(accounts)

    async def update_sort_order(self, account_ids: list[str], owner_id: int | None = None) -> int:
        """按传入账号ID顺序更新排序值。返回成功更新数量。"""
        normalized_ids = [item.strip() for item in account_ids if item and item.strip()]
        if not normalized_ids:
            return 0
        accounts = await self.get_accounts_for_user(owner_id, normalized_ids)
        account_map = {account.account_id: account for account in accounts}
        updated_count = 0
        for index, account_id in enumerate(normalized_ids, start=1):
            account = account_map.get(account_id)
            if account is None:
                continue
            account.sort_order = index
            self.session.add(account)
            updated_count += 1
        if updated_count:
            await self.session.commit()
        return updated_count

    async def update_auto_confirm(self, account: XYAccount, auto_confirm: bool) -> None:
        account.auto_confirm = auto_confirm
        self.session.add(account)
        await self.session.commit()

    async def update_pause_duration(self, account: XYAccount, duration: int) -> None:
        account.pause_duration = duration
        self.session.add(account)
        await self.session.commit()

    async def update_message_expire_time(self, account: XYAccount, expire_time: int) -> None:
        """更新相同消息等待时间"""
        account.message_expire_time = expire_time
        self.session.add(account)
        await self.session.commit()

    async def update_reply_delay(self, account: XYAccount, delay_seconds: int) -> None:
        """更新自动回复延迟时间(秒)"""
        account.reply_delay_seconds = delay_seconds
        self.session.add(account)
        await self.session.commit()

    async def update_login_info(
        self,
        account: XYAccount,
        username: str | None = None,
        login_password: str | None = None,
        show_browser: bool | None = None,
    ) -> None:
        """更新账号登录信息（用户名、密码、是否显示浏览器）"""
        if username is not None:
            account.username = username
        if login_password is not None:
            account.login_password = login_password
        if show_browser is not None:
            account.show_browser = show_browser
        self.session.add(account)
        await self.session.commit()

    async def update_scheduled_redelivery(self, account: XYAccount, scheduled_redelivery: bool) -> None:
        """更新定时补发货开关"""
        account.scheduled_redelivery = scheduled_redelivery
        self.session.add(account)
        await self.session.commit()

    async def update_scheduled_rate(self, account: XYAccount, scheduled_rate: bool) -> None:
        """更新定时补评价开关"""
        account.scheduled_rate = scheduled_rate
        self.session.add(account)
        await self.session.commit()

    async def delete_account(self, account: XYAccount) -> None:
        await self.session.delete(account)
        await self.session.commit()

    async def get_account_by_unb(self, owner_id: int, unb: str) -> XYAccount | None:
        stmt = select(XYAccount).where(
            XYAccount.owner_id == owner_id,
            XYAccount.unb == unb,
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def _generate_unique_account_id(self, owner_id: int, base: str) -> str:
        # 全局唯一：account_id 在整个系统内不允许重复，生成候选时不区分 owner_id
        normalized = base or f"qr_{int(datetime.utcnow().timestamp())}"
        stmt = select(XYAccount.account_id)
        result = await self.session.execute(stmt)
        existing_ids = set(result.scalars().all())
        candidate = normalized
        counter = 1
        while candidate in existing_ids:
            candidate = f"{normalized}_{counter}"
            counter += 1
        return candidate

    async def upsert_account_from_qr(
        self,
        owner_id: int,
        cookies: str,
        unb: str | None,
        *,
        login_method: str = "qr_scan",
    ) -> tuple[XYAccount, bool]:
        account: XYAccount | None = None
        if unb:
            account = await self.get_account_by_unb(owner_id, unb)

        created = False
        if account:
            account.cookie = cookies
            account.metadata_json = clear_cookie_refresh_snapshot(account.metadata_json)
            account.status = "active"
            account.disable_reason = None  # 清空禁用原因
            account.login_method = login_method
            account.unb = unb
            account.last_login_at = datetime.now(tz=UTC)
            if hasattr(account, "updated_at"):
                account.updated_at = datetime.now(tz=UTC)
        else:
            await AccountLimitService(self.session).ensure_can_add_account(owner_id)
            await self.ensure_category("默认", owner_id)
            base_id = unb or f"qr_{int(datetime.utcnow().timestamp())}"
            new_id = await self._generate_unique_account_id(owner_id, base_id)
            account = XYAccount(
                owner_id=owner_id,
                account_id=new_id,
                cookie=cookies,
                login_method=login_method,
                status="active",
                category="默认",
                sort_order=await self._top_sort_order("默认", owner_id),
                auto_confirm=False,
                pause_duration=10,
                show_browser=False,
                unb=unb,
                last_login_at=datetime.now(tz=UTC),
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
            self.session.add(account)
            created = True

        self.session.add(account)
        await self.session.commit()
        if created:
            await self.session.refresh(account)
        return account, created

    async def update_auto_polish(self, account: XYAccount, auto_polish: bool) -> None:
        """更新商品自动擦亮开关"""
        account.auto_polish = auto_polish
        self.session.add(account)
        await self.session.commit()

    async def update_confirm_before_send(self, account: XYAccount, confirm_before_send: bool) -> None:
        """更新发货成功再发卡券开关（与send_before_confirm互斥）"""
        account.confirm_before_send = confirm_before_send
        if confirm_before_send:
            account.send_before_confirm = False
        self.session.add(account)
        await self.session.commit()

    async def update_send_before_confirm(self, account: XYAccount, send_before_confirm: bool) -> None:
        """更新卡券发送成功再确认发货开关（与confirm_before_send互斥）"""
        account.send_before_confirm = send_before_confirm
        if send_before_confirm:
            account.confirm_before_send = False
        self.session.add(account)
        await self.session.commit()

    async def update_auto_red_flower(self, account: XYAccount, auto_red_flower: bool) -> None:
        """更新自动求小红花开关
        
        使用显式 UPDATE SQL 写入，避开 ORM 脏状态追踪可能的陷阱，
        确保操作一定会发送 UPDATE 语句到数据库。
        """
        stmt = (
            update(XYAccount)
            .where(XYAccount.id == account.id)
            .values(auto_red_flower=auto_red_flower)
        )
        await self.session.execute(stmt)
        await self.session.commit()
        # 同步内存对象属性（expire_on_commit=False 下对象属性不会自动刷新）
        account.auto_red_flower = auto_red_flower

    async def update_ai_reply_block_ordered_users(self, account: XYAccount, ai_reply_block_ordered_users: bool) -> None:
        """更新已下单用户禁止AI回复开关
        
        使用显式 UPDATE SQL 写入，确保操作一定会发送 UPDATE 语句到数据库。
        
        Args:
            account: 账号对象
            ai_reply_block_ordered_users: 是否禁止对已下单用户进行AI回复
        """
        stmt = (
            update(XYAccount)
            .where(XYAccount.id == account.id)
            .values(ai_reply_block_ordered_users=ai_reply_block_ordered_users)
        )
        await self.session.execute(stmt)
        await self.session.commit()
        # 同步内存对象属性（expire_on_commit=False 下对象属性不会自动刷新）
        account.ai_reply_block_ordered_users = ai_reply_block_ordered_users

    async def update_delivery_disabled(
        self,
        account: XYAccount,
        delivery_disabled: bool,
        delivery_disabled_reason: str | None,
        auto_close_order: bool = False,
        delivery_only_card_after_close: bool = False,
        excluded_item_ids: list[str] | None = None,
    ) -> None:
        """更新禁止发货设置（开关 + 原因 + 主动关闭订单 + 关闭后只发卡券 + 排除商品列表）

        使用显式 UPDATE SQL 写入，避免 ORM 脏状态追踪可能导致字段未落库。

        联动规则（与前端 UI 一致）：
          - 禁止发货关闭：reason / auto_close_order / delivery_only_card_after_close
            全部强制 False；排除商品列表强制清空
          - auto_close_order 关闭：delivery_only_card_after_close 强制 False
            （"关闭订单后继续发货"以"先关闭订单"为前置）

        Args:
            account: 账号实例
            delivery_disabled: 禁止发货开关
            delivery_disabled_reason: 禁止发货原因（开关关闭时会被清空）
            auto_close_order: 主动关闭订单开关
            delivery_only_card_after_close: 关闭订单后继续发货（仅发卡券）
            excluded_item_ids: 排除商品 item_id 列表（开关关闭时会被清空；列表内自动
                去重、去除空白、保留输入顺序）
        """
        normalized_reason: str | None
        # 排除列表归一化：去空白 + 去重 + 保持顺序；上限 500 个，避免 JSON 过大
        normalized_excluded: list[str] = []
        if excluded_item_ids:
            seen: set[str] = set()
            for raw in excluded_item_ids:
                if raw is None:
                    continue
                item_id = str(raw).strip()
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                normalized_excluded.append(item_id)
                if len(normalized_excluded) >= 500:
                    break

        if not delivery_disabled:
            normalized_reason = None
            normalized_auto_close = False
            normalized_only_card = False
            # 禁止发货关闭时，排除商品列表也强制清空，避免遗留无效配置
            normalized_excluded = []
        else:
            reason = (delivery_disabled_reason or "").strip()
            normalized_reason = reason or None
            normalized_auto_close = bool(auto_close_order)
            # 主动关闭订单关闭时，"关闭后只发卡券"必须强制关闭
            normalized_only_card = bool(delivery_only_card_after_close) if normalized_auto_close else False

        # JSON 字段写入：MySQL 接受 None 表示 NULL；空列表用 None 存以节省空间
        normalized_excluded_for_db: list[str] | None = normalized_excluded if normalized_excluded else None

        stmt = (
            update(XYAccount)
            .where(XYAccount.id == account.id)
            .values(
                delivery_disabled=delivery_disabled,
                delivery_disabled_reason=normalized_reason,
                auto_close_order=normalized_auto_close,
                delivery_only_card_after_close=normalized_only_card,
                delivery_disabled_excluded_items=normalized_excluded_for_db,
            )
        )
        await self.session.execute(stmt)

        # 同步写入新规则表（buyer_credit_zero 规则）
        from common.models.xy_delivery_block_rule import XYDeliveryBlockRule
        from sqlalchemy import and_

        rule_stmt = select(XYDeliveryBlockRule).where(
            and_(
                XYDeliveryBlockRule.account_id == account.account_id,
                XYDeliveryBlockRule.rule_code == "buyer_credit_zero",
            )
        )
        result = await self.session.execute(rule_stmt)
        existing_rule = result.scalars().first()

        if existing_rule:
            existing_rule.enabled = delivery_disabled
            existing_rule.block_reason = normalized_reason
            existing_rule.auto_close_order = normalized_auto_close
            existing_rule.only_card_after_close = normalized_only_card
            existing_rule.excluded_item_ids = normalized_excluded_for_db
        else:
            new_rule = XYDeliveryBlockRule(
                account_id=account.account_id,
                rule_code="buyer_credit_zero",
                enabled=delivery_disabled,
                priority=10,
                block_reason=normalized_reason,
                auto_close_order=normalized_auto_close,
                only_card_after_close=normalized_only_card,
                excluded_item_ids=normalized_excluded_for_db,
                config={"threshold": 0},
            )
            self.session.add(new_rule)

        await self.session.commit()
        # 同步内存对象属性
        account.delivery_disabled = delivery_disabled
        account.delivery_disabled_reason = normalized_reason
        account.auto_close_order = normalized_auto_close
        account.delivery_only_card_after_close = normalized_only_card
        account.delivery_disabled_excluded_items = normalized_excluded_for_db

    async def get_delivery_block_rules(self, account_id: str) -> list[dict]:
        """获取账号的禁止发货规则列表

        返回该账号所有规则配置（包括未启用的），按 priority 排序。
        如果账号在 xy_delivery_block_rules 表中没有记录，返回所有可用规则的默认配置。

        Args:
            account_id: 账号标识（xy_accounts.account_id）

        Returns:
            规则配置列表
        """
        from common.models.xy_delivery_block_rule import XYDeliveryBlockRule
        from common.services.delivery_block_rule_meta import get_all_rule_metadata

        # 查询已有规则
        stmt = (
            select(XYDeliveryBlockRule)
            .where(XYDeliveryBlockRule.account_id == account_id)
            .order_by(XYDeliveryBlockRule.priority.asc())
        )
        result = await self.session.execute(stmt)
        existing_rules = result.scalars().all()

        # 构建已有规则的 code 集合
        existing_codes = {r.rule_code for r in existing_rules}

        # 获取所有可用规则元信息
        all_metadata = get_all_rule_metadata()

        # 合并：已有规则 + 未配置的规则（用默认值填充）
        rule_list = []
        for rule in existing_rules:
            # 归一化 excluded_item_ids
            excluded = []
            if rule.excluded_item_ids:
                raw = rule.excluded_item_ids
                if isinstance(raw, str):
                    try:
                        import json
                        raw = json.loads(raw)
                    except Exception:
                        raw = []
                if isinstance(raw, list):
                    excluded = [str(x).strip() for x in raw if x is not None and str(x).strip()]

            rule_list.append({
                "rule_code": rule.rule_code,
                "rule_name": next(
                    (m["rule_name"] for m in all_metadata if m["rule_code"] == rule.rule_code),
                    rule.rule_code,
                ),
                "rule_description": next(
                    (m["rule_description"] for m in all_metadata if m["rule_code"] == rule.rule_code),
                    "",
                ),
                "enabled": rule.enabled,
                "priority": rule.priority,
                "block_reason": rule.block_reason or "",
                "auto_close_order": bool(rule.auto_close_order),
                "only_card_after_close": bool(rule.only_card_after_close),
                "excluded_item_ids": excluded,
                "config": rule.config or {},
                "default_config": next(
                    (m["default_config"] for m in all_metadata if m["rule_code"] == rule.rule_code),
                    {},
                ),
            })

        # 补充未配置的规则（默认关闭）
        for meta in all_metadata:
            if meta["rule_code"] not in existing_codes:
                rule_list.append({
                    "rule_code": meta["rule_code"],
                    "rule_name": meta["rule_name"],
                    "rule_description": meta["rule_description"],
                    "enabled": False,
                    "priority": meta["default_priority"],
                    "block_reason": "",
                    "auto_close_order": False,
                    "only_card_after_close": False,
                    "excluded_item_ids": [],
                    "config": meta["default_config"],
                    "default_config": meta["default_config"],
                })

        # 按 priority 排序
        rule_list.sort(key=lambda x: x["priority"])
        return rule_list

    async def update_delivery_block_rules(
        self,
        account_id: str,
        rules: list,
    ) -> None:
        """批量更新账号的禁止发货规则配置

        使用 UPSERT 逻辑：存在则更新，不存在则插入。

        Args:
            account_id: 账号标识（xy_accounts.account_id）
            rules: 规则配置列表（DeliveryBlockRuleItem 实例列表）
        """
        from common.models.xy_delivery_block_rule import XYDeliveryBlockRule
        from sqlalchemy import and_

        for rule_item in rules:
            rule_code = rule_item.rule_code
            enabled = rule_item.enabled
            priority = rule_item.priority
            block_reason = (rule_item.block_reason or "").strip() or None
            auto_close = rule_item.auto_close_order
            only_card = rule_item.only_card_after_close if auto_close else False

            # 归一化排除商品列表
            excluded_list: list[str] = []
            if rule_item.excluded_item_ids:
                seen: set[str] = set()
                for raw in rule_item.excluded_item_ids:
                    if raw is None:
                        continue
                    item_id = str(raw).strip()
                    if not item_id or item_id in seen:
                        continue
                    seen.add(item_id)
                    excluded_list.append(item_id)
                    if len(excluded_list) >= 500:
                        break
            excluded_for_db = excluded_list if excluded_list else None

            # 规则参数
            config = rule_item.config if rule_item.config else None

            # 查询是否已存在
            stmt = select(XYDeliveryBlockRule).where(
                and_(
                    XYDeliveryBlockRule.account_id == account_id,
                    XYDeliveryBlockRule.rule_code == rule_code,
                )
            )
            result = await self.session.execute(stmt)
            existing = result.scalars().first()

            if existing:
                # 更新
                existing.enabled = enabled
                existing.priority = priority
                existing.block_reason = block_reason
                existing.auto_close_order = auto_close
                existing.only_card_after_close = only_card
                existing.excluded_item_ids = excluded_for_db
                existing.config = config
            else:
                # 插入
                new_rule = XYDeliveryBlockRule(
                    account_id=account_id,
                    rule_code=rule_code,
                    enabled=enabled,
                    priority=priority,
                    block_reason=block_reason,
                    auto_close_order=auto_close,
                    only_card_after_close=only_card,
                    excluded_item_ids=excluded_for_db,
                    config=config,
                )
                self.session.add(new_rule)

        # 同步更新旧字段 delivery_disabled（用于前端图标显示兼容）
        # 只要有任何一条规则 enabled=True，旧字段就标记为 True
        has_any_enabled = any(r.enabled for r in rules)
        sync_stmt = (
            update(XYAccount)
            .where(XYAccount.account_id == account_id)
            .values(delivery_disabled=has_any_enabled)
        )
        await self.session.execute(sync_stmt)

        await self.session.commit()
