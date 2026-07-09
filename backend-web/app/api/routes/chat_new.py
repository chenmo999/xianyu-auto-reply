"""
在线聊天(新) API路由

功能：
1. 获取当前用户的账号列表（供前端选择）
2. 连接/断开指定账号的IM
3. 获取会话列表
4. 获取聊天记录
5. 获取已连接账号列表
6. WebSocket接口，实时推送IM消息给前端

与自动回复WebSocket隔离，使用独立的token和device_id
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from types import SimpleNamespace
from urllib.parse import unquote

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel

from app.api.deps import get_current_active_user, get_db_session
from app.services.chat_new import get_im_session_manager
from app.services.chat_new.avatar_service import get_owner_user_info, get_user_info, AVATAR_CACHE_PREFIX, AVATAR_CACHE_TTL
from app.services.chat_new.official_blacklist_service import official_blacklist_request
from common.db.redis_client import get_redis_client
from common.models import User, XYAccount, XYOrder, XYCatalogItem
from common.models.product_material import ProductMaterial
from common.models.publish_log import PublishLog
from common.schemas.common import ApiResponse
from common.utils.auth_scope import is_admin_user
from common.utils.xianyu_utils import trans_cookies, extract_account_user_id_from_cookie
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession


# 无效昵称黑名单（系统生成的非真实用户昵称 / 系统消息摘要）
_INVALID_NICK_SET = {
    "交易消息", "系统消息", "卡片消息",
    "我完成了评价", "对方完成了评价", "快给ta一个评价吧～",
    "卖家已发货", "买家已付款", "买家已确认收货", "等待您发货",
    "超时未付款，系统关闭了订单",
}


def _is_valid_nick(name: str) -> bool:
    """
    检查昵称是否有效（非空、非纯数字、非系统昵称）

    过滤规则：
    1. 空值或纯空白
    2. 纯数字（可能是用户ID误当昵称）
    3. 精确匹配系统消息摘要黑名单
    4. 被方括号包裹的文本（如"[卡片消息]"去括号后匹配）
    """
    if not name or not name.strip():
        return False
    stripped = name.strip()
    if stripped.isdigit():
        return False
    if stripped in _INVALID_NICK_SET:
        return False
    # 处理可能带方括号的系统消息摘要，如 "[我完成了评价]"
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1]
        if inner in _INVALID_NICK_SET:
            return False
    return True

router = APIRouter(prefix="/chat-new")


@router.get("/accounts")
async def list_accounts(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    获取账号列表（管理员查看所有账号，普通用户只看自己的），支持分页
    """
    try:
        is_admin = is_admin_user(current_user)

        # 基础条件：有cookie的账号
        base_where = [XYAccount.cookie.isnot(None), XYAccount.cookie != ""]
        if not is_admin:
            base_where.append(XYAccount.owner_id == current_user.id)

        # 查总数
        count_q = select(func.count()).select_from(XYAccount).where(*base_where)
        total = (await db.execute(count_q)).scalar() or 0

        # 分页查询，按状态排序（active在前）
        query = (
            select(XYAccount)
            .where(*base_where)
            .order_by(
                # active排前面
                func.IF(XYAccount.status == "active", 0, 1),
                XYAccount.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await db.execute(query)
        accounts = result.scalars().all()

        # 管理员场景批量查用户名
        owner_map: dict[int, str] = {}
        if is_admin:
            owner_ids = list({acc.owner_id for acc in accounts if acc.owner_id})
            if owner_ids:
                user_result = await db.execute(
                    select(User).where(User.id.in_(owner_ids))
                )
                for u in user_result.scalars().all():
                    owner_map[u.id] = u.username or str(u.id)

        manager = get_im_session_manager()
        connected_ids = manager.get_connected_account_ids()

        items = []
        display_names_updated = False
        for acc in accounts:
            display_name = acc.display_name or ""
            if not display_name and acc.cookie:
                try:
                    tracknick = trans_cookies(acc.cookie).get("tracknick")
                    if tracknick:
                        display_name = unquote(tracknick)
                        acc.display_name = display_name
                        display_names_updated = True
                except Exception as e:
                    logger.warning(f"账号 {acc.account_id} 解析昵称失败: {e}")
            item = {
                "account_id": acc.account_id,
                "display_name": display_name,
                "remark": acc.remark or "",
                "connected": acc.account_id in connected_ids,
                "status": acc.status or "active",
            }
            if is_admin:
                item["owner"] = owner_map.get(acc.owner_id, str(acc.owner_id))
            items.append(item)
        if display_names_updated:
            await db.commit()

        return {
            "success": True,
            "data": items,
            "total": total,
            "hasMore": page * page_size < total,
        }

    except Exception as e:
        logger.error(f"获取账号列表失败: {e}")
        return ApiResponse(success=False, message=f"获取账号列表失败: {str(e)}")


@router.post("/connect/{account_id}")
async def connect_account(
    account_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    连接指定账号的IM WebSocket
    """
    try:
        # 校验账号归属（管理员可操作任意账号）
        query = select(XYAccount).where(XYAccount.account_id == account_id)
        if not is_admin_user(current_user):
            query = query.where(XYAccount.owner_id == current_user.id)
        result = await db.execute(query)
        account = result.scalar_one_or_none()
        if not account:
            return ApiResponse(success=False, message="账号不存在或无权操作")

        manager = get_im_session_manager()
        await manager.get_or_connect(account_id)
        return ApiResponse(success=True, message="连接成功")

    except ValueError as e:
        return ApiResponse(success=False, message=str(e))
    except Exception as e:
        logger.error(f"【{account_id}】连接IM失败: {e}")
        return ApiResponse(success=False, message=f"连接失败: {str(e)}")


@router.post("/disconnect/{account_id}")
async def disconnect_account(
    account_id: str,
    current_user: User = Depends(get_current_active_user),
):
    """
    断开指定账号的IM WebSocket
    """
    try:
        manager = get_im_session_manager()
        await manager.disconnect(account_id)
        return ApiResponse(success=True, message="已断开连接")

    except Exception as e:
        logger.error(f"【{account_id}】断开IM失败: {e}")
        return ApiResponse(success=False, message=f"断开失败: {str(e)}")


@router.get("/conversations/{account_id}")
async def get_conversations(
    account_id: str,
    cursor: int = None,
    limit: int = 20,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    获取指定账号的会话列表

    Args:
        account_id: 账号ID
        cursor: 分页游标（首页不传，翻页传nextCursor）
        limit: 每页数量，默认20
    """
    try:
        manager = get_im_session_manager()
        client = manager.clients.get(account_id)
        if not client or not client.is_connected:
            return ApiResponse(success=False, message="账号未连接，请先连接")

        body = await client.get_conversations(
            start_timestamp=cursor, limit=limit
        )

        # 检测IM错误响应（流控或其他服务端错误）
        if isinstance(body, dict) and "reason" in body:
            reason = body.get("reason", "")
            err_code = body.get("code", "")
            logger.warning(
                f"【{account_id}】IM会话列表返回错误, "
                f"code={err_code}, reason={reason}"
            )
            # 流控错误给前端提示
            if err_code == "400600001":
                return ApiResponse(success=False, message="请求过于频繁，请稍后再试")
            return ApiResponse(success=False, message=f"IM服务异常: {reason or err_code}")

        # 解析会话列表
        conversations = []
        user_convs = body.get("userConvs", [])
        for item in user_convs:
            # 实际会话数据嵌套在 singleChatUserConversation 中
            conv = item.get("singleChatUserConversation", item) if isinstance(item, dict) else item
            conv_info = _parse_conversation(conv, client.myid)
            if conv_info:
                # v1.3.8：这里必须直接从 IM 原始会话对象里补“黄色商品标题/itemId”。
                # 之前只依赖 _parse_conversation 的 itemTitle，某些刷新路径里会导致后续城市匹配逻辑不触发。
                try:
                    raw_title = (_extract_item_title_from_any(item) or _extract_item_title_from_any(conv) or "")[:200]
                    raw_item_id = _extract_item_id_from_any(item) or _extract_item_id_from_any(conv) or ""
                    if raw_title and not conv_info.get("itemTitle"):
                        conv_info["itemTitle"] = raw_title
                    if raw_item_id and not conv_info.get("itemId"):
                        conv_info["itemId"] = raw_item_id
                except Exception as e:
                    logger.info(f"【{account_id}】v1.3.8 原始会话补黄色标题失败: {e}")
                conversations.append(conv_info)

        # v1.3.8：强制记录会话刷新入口，确认本次刷新是否真的进入“黄色标题补城市”逻辑。
        try:
            yellow_count = sum(1 for c in conversations if str(c.get("itemTitle") or "").strip())
            sample_titles = [str(c.get("itemTitle") or "")[:60] for c in conversations if str(c.get("itemTitle") or "").strip()][:5]
            sample_ids = [str(c.get("itemId") or "") for c in conversations if str(c.get("itemId") or "").strip()][:5]
            logger.info(
                f"【{account_id}】v1.3.8 会话刷新入口：raw={len(user_convs)}, "
                f"parsed={len(conversations)}, yellowTitle={yellow_count}, "
                f"sampleTitles={sample_titles}, sampleItemIds={sample_ids}"
            )
        except Exception:
            pass

        # v1.4.5：输入工具栏“咨询商品”。
        # 只根据会话黄色标题 / itemId 去当前账号“在售”商品列表匹配；
        # 匹配到才给前端 consultItemUrl，未在售/已下架的不显示。
        try:
            await _attach_consulting_item_links(
                db=db,
                current_user=current_user,
                account_id=account_id,
                conversations=conversations,
            )
        except Exception as e:
            logger.warning(f"【{account_id}】v1.4.5 客户正在咨询商品链接匹配失败: {e}")

        # v1.4.2：城市/商品发布地标签已停用。
        # 保留黄色商品标题解析，但不再反查发布地，也不再给会话写 buyerCity，避免无效请求和误显示。

        # 从 Redis 批量读取所有会话的缓存，按优先级确定昵称并回写更优数据
        try:
            redis_client = await get_redis_client()
            read_pipe = redis_client.pipeline()
            for c in conversations:
                read_pipe.get(f"{AVATAR_CACHE_PREFIX}{c['cid']}")
            results = await read_pipe.execute()

            write_pipe = redis_client.pipeline()
            need_write = False

            for c, cached_raw in zip(conversations, results):
                # 解析缓存数据
                cached_data = {}
                if cached_raw:
                    try:
                        parsed = json.loads(cached_raw) if isinstance(cached_raw, str) else cached_raw
                        if isinstance(parsed, str):
                            cached_data = {"avatar": parsed, "nick": ""}
                        elif isinstance(parsed, dict):
                            cached_data = parsed
                    except (json.JSONDecodeError, TypeError):
                        cached_data = {}

                cached_nick = cached_data.get("nick", "")
                cached_avatar = cached_data.get("avatar", "")
                cached_city = ""  # v1.4.2：停用城市标签，不再从头像缓存恢复城市
                conv_city = ""
                conv_nick = c.get("otherUserName", "")  # 从会话列表 reminderTitle 提取的

                # v1.4.2：城市标签停用，不再从缓存补 buyerCity。

                # 昵称优先级：会话列表有效昵称 > 缓存昵称
                if _is_valid_nick(conv_nick):
                    # 会话列表有有效昵称
                    # 如果缓存无昵称或缓存昵称带***而会话列表不带，则更新缓存
                    if not cached_nick or ("***" in cached_nick and "***" not in conv_nick):
                        new_cache = {"avatar": cached_avatar, "nick": conv_nick}
                        if conv_city:
                            new_cache["city"] = conv_city
                            new_cache["cityChecked"] = True
                        elif isinstance(cached_data, dict) and "cityChecked" in cached_data:
                            new_cache["cityChecked"] = cached_data.get("cityChecked")
                        write_pipe.set(
                            f"{AVATAR_CACHE_PREFIX}{c['cid']}",
                            json.dumps(new_cache, ensure_ascii=False),
                            ex=AVATAR_CACHE_TTL,
                        )
                        need_write = True
                else:
                    # 会话列表无有效昵称，使用缓存昵称
                    if cached_nick:
                        c["otherUserName"] = cached_nick

                # 头像：缓存有头像且会话没有时补填
                if cached_avatar and not c.get("otherUserAvatar"):
                    c["otherUserAvatar"] = cached_avatar

                # 如果订单地址补到了城市，但缓存里还没有城市，单独回写缓存
                if conv_city and conv_city != cached_city:
                    city_cache = dict(cached_data) if isinstance(cached_data, dict) else {}
                    city_cache["avatar"] = city_cache.get("avatar", cached_avatar or c.get("otherUserAvatar", ""))
                    city_cache["nick"] = city_cache.get("nick", cached_nick or c.get("otherUserName", ""))
                    city_cache["city"] = conv_city
                    city_cache["cityChecked"] = True
                    write_pipe.set(
                        f"{AVATAR_CACHE_PREFIX}{c['cid']}",
                        json.dumps(city_cache, ensure_ascii=False),
                        ex=AVATAR_CACHE_TTL,
                    )
                    need_write = True

            if need_write:
                await write_pipe.execute()

        except Exception as e:
            logger.warning(f"【{account_id}】Redis批量处理用户信息失败: {e}")

        # v1.4.2：最终返回前清理 buyerCity，彻底关闭城市标签。
        try:
            for _c in conversations:
                if isinstance(_c, dict):
                    _c.pop("buyerCity", None)
        except Exception:
            pass

        return ApiResponse(
            success=True,
            data={
                "conversations": conversations,
                "hasMore": body.get("hasMore", False),
                "nextCursor": body.get("nextCursor", None),
            },
        )

    except Exception as e:
        logger.error(f"【{account_id}】获取会话列表失败: {e}")
        return ApiResponse(success=False, message=f"获取会话列表失败: {str(e)}")


@router.get("/messages/{account_id}/{cid}")
async def get_messages(
    account_id: str,
    cid: str,
    cursor: int = None,
    limit: int = 20,
    current_user: User = Depends(get_current_active_user),
):
    """
    获取指定会话的聊天记录

    Args:
        account_id: 账号ID
        cid: 会话ID
        cursor: 分页游标（首页不传，翻页传nextCursor）
        limit: 每页数量，默认20
    """
    try:
        manager = get_im_session_manager()
        client = manager.clients.get(account_id)
        if not client or not client.is_connected:
            return ApiResponse(success=False, message="账号未连接，请先连接")

        body = await client.get_messages(
            cid=cid, start_timestamp=cursor, limit=limit
        )

        # 检测IM错误响应（可能是瞬态问题，返回空数据让轮询重试）
        if isinstance(body, dict) and "reason" in body:
            err_msg = body.get("developerMessage", body.get("reason", ""))
            logger.warning(f"【{account_id}】IM消息列表返回错误: {err_msg}，等待下次轮询重试")
            return ApiResponse(
                success=True,
                data={"messages": [], "hasMore": False, "nextCursor": None},
            )

        # 解析消息列表（IM返回倒序，需反转为正序：最旧在前，最新在后）
        messages = []
        models = body.get("userMessageModels", [])
        for model in models:
            msg_info = _parse_message(model, client.myid)
            if msg_info:
                messages.append(msg_info)
        messages.reverse()

        return ApiResponse(
            success=True,
            data={
                "messages": messages,
                "hasMore": body.get("hasMore", False)
                    if isinstance(body.get("hasMore"), bool)
                    else body.get("hasMore", 0) == 1,
                "nextCursor": body.get("nextCursor", None),
            },
        )

    except Exception as e:
        logger.error(f"【{account_id}】获取聊天记录失败: {e}")
        return ApiResponse(success=False, message=f"获取聊天记录失败: {str(e)}")


# ==================== 发送消息请求模型 ====================


class SendMessageRequest(BaseModel):
    """发送消息请求体"""
    cid: str
    toUserId: str
    text: str


class RecallMessageRequest(BaseModel):
    messageId: str
    messageTime: int


class AvatarQueryItem(BaseModel):
    """单条头像查询项"""
    userId: str
    cid: str


class AvatarQueryRequest(BaseModel):
    """批量查询头像请求体"""
    queries: list[AvatarQueryItem]


@router.post("/send-message/{account_id}")
async def send_message(
    account_id: str,
    req: SendMessageRequest,
    current_user: User = Depends(get_current_active_user),
):
    """
    发送文本消息

    Args:
        account_id: 账号ID
        req: 包含 cid（会话ID）、toUserId（对方用户ID）、text（消息内容）
    """
    try:
        manager = get_im_session_manager()
        client = manager.clients.get(account_id)
        if not client or not client.is_connected:
            return ApiResponse(success=False, message="账号未连接，请先连接")

        if not req.text.strip():
            return ApiResponse(success=False, message="消息内容不能为空")

        send_result = await client.send_text_message(
            cid=req.cid,
            to_user_id=req.toUserId,
            text=req.text,
        )
        logger.info(
            f"【{account_id}】发送消息到 {req.toUserId}: {req.text[:50]}"
        )
        return ApiResponse(
            success=True,
            message="发送成功",
            data={"messageId": send_result.get("messageId", "")},
        )

    except Exception as e:
        # send_text_message 在被 IM 安全拦截等业务错误时会抛出明文原因，
        # 直接透传给前端展示（如"内容存在不当信息..."），便于用户调整后重发。
        logger.warning(f"【{account_id}】发送消息失败: {e}")
        return ApiResponse(success=False, message=f"发送失败：{str(e)}")


@router.post("/recall-message/{account_id}")
async def recall_message(
    account_id: str,
    req: RecallMessageRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not await _get_owned_chat_account(account_id, current_user, db):
        return ApiResponse(success=False, message="账号不存在或无权操作")
    client = get_im_session_manager().clients.get(account_id)
    if not client or not client.is_connected:
        return ApiResponse(success=False, message="账号未连接")
    if not req.messageId:
        return ApiResponse(success=False, message="缺少消息ID，无法撤回")
    message_time_ms = req.messageTime * 1000 if req.messageTime < 1_000_000_000_000 else req.messageTime
    elapsed_ms = int(time.time() * 1000) - message_time_ms
    if elapsed_ms < -10_000 or elapsed_ms > 120_000:
        return ApiResponse(success=False, message="消息发送超过两分钟，无法撤回")
    try:
        await client.recall_message(req.messageId)
        return ApiResponse(success=True, message="消息已撤回")
    except Exception as e:
        logger.error(f"【{account_id}】撤回消息失败: {e}")
        return ApiResponse(success=False, message=f"撤回失败: {e}")


async def _get_owned_chat_account(account_id: str, current_user: User, db: AsyncSession) -> XYAccount | None:
    query = select(XYAccount).where(XYAccount.account_id == account_id)
    if not is_admin_user(current_user):
        query = query.where(XYAccount.owner_id == current_user.id)
    return (await db.execute(query)).scalar_one_or_none()


@router.get("/official-blacklist/{account_id}/{cid}")
async def query_official_blacklist(
    account_id: str,
    cid: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    account = await _get_owned_chat_account(account_id, current_user, db)
    if not account or not account.cookie:
        return ApiResponse(success=False, message="账号不存在或Cookie为空")
    try:
        data = await official_blacklist_request(account.cookie, cid, "query")
        return ApiResponse(success=True, data={"blocked": bool(data.get("isInBlack"))})
    except Exception as e:
        return ApiResponse(success=False, message=f"查询黑名单状态失败: {e}")


@router.post("/official-blacklist/{account_id}/{cid}/{action}")
async def change_official_blacklist(
    account_id: str,
    cid: str,
    action: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    if action not in {"add", "remove"}:
        return ApiResponse(success=False, message="无效操作")
    account = await _get_owned_chat_account(account_id, current_user, db)
    if not account or not account.cookie:
        return ApiResponse(success=False, message="账号不存在或Cookie为空")
    try:
        await official_blacklist_request(account.cookie, cid, action)
        blocked = action == "add"
        return ApiResponse(
            success=True,
            message="已加入闲鱼官方黑名单" if blocked else "已解除闲鱼官方黑名单",
            data={"blocked": blocked},
        )
    except Exception as e:
        return ApiResponse(success=False, message=f"黑名单操作失败: {e}")


@router.post("/avatars/{account_id}")
async def query_avatars(
    account_id: str,
    req: AvatarQueryRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """
    批量查询用户头像

    优先从Redis缓存获取，缓存未命中时调用mtop API查询，结果缓存24小时

    Args:
        account_id: 账号ID
        req: 包含 session_ids 列表
    """
    try:
        # 校验账号归属并获取cookie（管理员可操作任意账号）
        query = select(XYAccount).where(XYAccount.account_id == account_id)
        if not is_admin_user(current_user):
            query = query.where(XYAccount.owner_id == current_user.id)
        result = await db.execute(query)
        account = result.scalar_one_or_none()
        if not account or not account.cookie:
            return ApiResponse(success=False, message="账号不存在或Cookie为空")

        user_infos = {}
        for idx, item in enumerate(req.queries):
            # 每次请求间隔 0.3 秒，防止 mtop API 限流
            if idx > 0:
                await asyncio.sleep(0.3)
            info = await get_user_info(
                account_id=account_id,
                cid=item.cid,
                cookies_str=account.cookie,
                db=db,
            )
            if info:
                user_infos[item.userId] = info

        return ApiResponse(success=True, data=user_infos)

    except Exception as e:
        logger.error(f"【{account_id}】批量查询头像失败: {e}")
        return ApiResponse(success=False, message=f"查询头像失败: {str(e)}")


# ==================== 消息解析辅助函数 ====================


@router.get("/account-profile/{account_id}")
async def get_account_profile(
    account_id: str,
    cid: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db_session),
):
    """查询并持久化卖家在闲鱼的真实昵称"""
    query = select(XYAccount).where(XYAccount.account_id == account_id)
    if not is_admin_user(current_user):
        query = query.where(XYAccount.owner_id == current_user.id)
    account = (await db.execute(query)).scalar_one_or_none()
    if not account or not account.cookie:
        return ApiResponse(success=False, message="账号不存在或Cookie为空")

    info = await get_owner_user_info(account_id, cid, account.cookie, db)
    if info and _is_valid_nick(info.get("nick", "")):
        account.display_name = info["nick"]
        await db.commit()
    return ApiResponse(success=True, data=info or {})




async def _attach_consulting_item_links(
    db: AsyncSession,
    current_user: User,
    account_id: str,
    conversations: list[dict],
) -> None:
    """v1.4.5：给会话补输入工具栏“咨询商品”的在售商品链接。

    只做一件事：黄色标题 / itemId -> 当前账号在售商品列表 -> consultItemUrl。
    不查城市、不扫详情页、不处理已下架商品。
    """
    if not conversations:
        return

    consult_infos: list[dict[str, str]] = []
    titles: list[str] = []
    item_ids: list[str] = []

    for c in conversations:
        title = str(c.get("itemTitle") or "").strip()[:200]
        item_id = str(c.get("itemId") or "").strip()
        if not title and not item_id:
            continue
        consult_infos.append({"title": title, "item_id": item_id})
        if title:
            titles.append(title)
        if item_id:
            item_ids.append(item_id)

    titles = list(dict.fromkeys([v for v in titles if v]))
    item_ids = list(dict.fromkeys([v for v in item_ids if v]))
    if not titles and not item_ids:
        return

    match_result = await _get_in_stock_consult_item_matches(
        db=db,
        current_user=current_user,
        account_id=account_id,
        titles=titles,
        item_ids=item_ids,
    )
    by_item_id: dict[str, dict] = match_result.get("by_item_id") or {}
    by_title: dict[str, dict] = match_result.get("by_title") or {}

    attached = 0
    skipped = []
    for c in conversations:
        title = str(c.get("itemTitle") or "").strip()[:200]
        item_id = str(c.get("itemId") or "").strip()
        matched = None
        if item_id and item_id in by_item_id:
            matched = by_item_id.get(item_id)
        if not matched and title and title in by_title:
            matched = by_title.get(title)
        if not matched:
            if (title or item_id) and len(skipped) < 8:
                skipped.append({"itemId": item_id, "title": title})
            continue

        matched_item_id = str(matched.get("item_id") or item_id or "").strip()
        matched_title = str(matched.get("title") or title or "").strip()[:200]
        item_url = str(matched.get("item_url") or "").strip()
        if not item_url and matched_item_id:
            item_url = f"https://www.goofish.com/item?id={matched_item_id}"
        if not item_url:
            continue

        c["consultItemId"] = matched_item_id
        c["consultItemTitle"] = matched_title
        c["consultItemUrl"] = item_url
        attached += 1

    if attached:
        samples = []
        for c in conversations:
            if c.get("consultItemUrl"):
                samples.append(f"{c.get('consultItemId') or ''}:{str(c.get('consultItemTitle') or '')[:30]}")
                if len(samples) >= 5:
                    break
        logger.info(f"【{account_id}】v1.4.5 已给客户正在咨询补在售商品链接：{attached} 条，samples={samples}")
    if skipped:
        logger.info(f"【{account_id}】v1.4.5 咨询商品未在当前在售列表，输入工具栏不显示：{skipped}")


async def _get_in_stock_consult_item_matches(
    db: AsyncSession,
    current_user: User,
    account_id: str,
    titles: list[str],
    item_ids: list[str] | None = None,
) -> dict[str, dict]:
    """v1.4.5：用商品管理同一套在售列表接口，按标题 / itemId 找当前咨询商品。"""
    clean_titles = list(dict.fromkeys([str(t or "").strip()[:200] for t in titles if str(t or "").strip()]))
    clean_item_ids = list(dict.fromkeys([str(i or "").strip() for i in (item_ids or []) if str(i or "").strip()]))
    by_item_id: dict[str, dict] = {}
    by_title: dict[str, dict] = {}

    if not clean_titles and not clean_item_ids:
        return {"by_item_id": by_item_id, "by_title": by_title}

    try:
        account_query = select(XYAccount).where(XYAccount.account_id == account_id)
        if not is_admin_user(current_user):
            account_query = account_query.where(XYAccount.owner_id == current_user.id)
        account = (await db.execute(account_query)).scalar_one_or_none()
        if not account or not account.cookie:
            logger.info(f"【{account_id}】v1.4.5 在售咨询商品匹配跳过：账号不存在或 Cookie 为空")
            return {"by_item_id": by_item_id, "by_title": by_title}

        from common.utils.item_info_manager import ItemInfoManager

        myid = extract_account_user_id_from_cookie(account.cookie) or str(account.unb or "").strip() or str(account.account_id or "").strip()
        manager = ItemInfoManager(account.account_id, account.cookie)
        try:
            page_size_candidates = [20, 10, 5]
            loaded_any_page = False
            for page_size in page_size_candidates:
                max_pages = 6 if page_size >= 20 else 10
                for page in range(1, max_pages + 1):
                    result = await manager.get_item_list_info(page_number=page, page_size=page_size, myid=myid)
                    if not result or not result.get("success"):
                        logger.info(f"【{account_id}】v1.4.5 在售咨询商品第{page}页失败：pageSize={page_size}, result={result}")
                        break

                    loaded_any_page = True
                    items = result.get("items") or []
                    raw_cards = ((result.get("raw_data") or {}).get("cardList") or []) if isinstance(result.get("raw_data"), dict) else []
                    if not items and not raw_cards:
                        break

                    candidates: list[tuple[dict, dict]] = []
                    for item in items:
                        if isinstance(item, dict):
                            candidates.append((item, {}))
                    for card in raw_cards:
                        if not isinstance(card, dict):
                            continue
                        card_data = card.get("cardData") or {}
                        if isinstance(card_data, dict):
                            candidates.append((card_data, card))

                    page_seen: list[str] = []
                    for item, raw_card in candidates:
                        if not isinstance(item, dict):
                            continue
                        item_id = str(item.get("id") or item.get("item_id") or item.get("itemId") or "").strip()
                        title = str(item.get("title") or item.get("item_title") or item.get("itemTitle") or "").strip()[:200]
                        if item_id or title:
                            page_seen.append(f"{item_id}:{title[:30]}")
                        if not item_id and not title:
                            continue

                        hit_titles = [t for t in clean_titles if title and (title == t or _title_rough_match(t, title))]
                        hit_id = bool(item_id and item_id in clean_item_ids)
                        if not hit_titles and not hit_id:
                            continue

                        item_url = str(
                            item.get("item_url")
                            or item.get("itemUrl")
                            or item.get("url")
                            or item.get("href")
                            or raw_card.get("item_url")
                            or raw_card.get("itemUrl")
                            or raw_card.get("url")
                            or ""
                        ).strip()
                        if not item_url and item_id:
                            item_url = f"https://www.goofish.com/item?id={item_id}"

                        payload = {"item_id": item_id, "title": title, "item_url": item_url}
                        if hit_id and item_id:
                            by_item_id.setdefault(item_id, payload)
                        for t in hit_titles:
                            by_title.setdefault(t, payload)

                    if page_seen:
                        logger.info(f"【{account_id}】v1.4.5 当前在售咨询商品第{page}页样例：{page_seen[:8]}")

                    all_ids_matched = not clean_item_ids or set(clean_item_ids).issubset(set(by_item_id.keys()))
                    all_titles_matched = not clean_titles or set(clean_titles).issubset(set(by_title.keys()))
                    if all_ids_matched and all_titles_matched:
                        break
                    if len(items) < page_size:
                        break
                    await asyncio.sleep(0.25)

                if loaded_any_page:
                    break
        finally:
            await manager.close()

        logger.info(
            f"【{account_id}】v1.4.5 当前在售咨询商品匹配结果："
            f"titleMatches={len(by_title)}, itemIdMatches={len(by_item_id)}"
        )
        return {"by_item_id": by_item_id, "by_title": by_title}
    except Exception as e:
        logger.warning(f"【{account_id}】v1.4.5 在售咨询商品匹配异常: {e}")
        return {"by_item_id": by_item_id, "by_title": by_title}


async def _get_buyer_city_map_from_consult_items(
    db: AsyncSession,
    current_user: User,
    account_id: str,
    conversations: list[dict],
) -> dict[str, str]:
    """v1.4.0：只给当前“在售”商品显示发布地城市标签。

    规则：
    1. 只读取会话列表黄色商品标题 / itemId；
    2. 只拉取当前卖家账号“在售”商品列表匹配；
    3. 咨询商品不在“在售”列表里，直接跳过，不显示城市标签；
    4. 在售商品匹配到后，优先从在售商品原始卡片取发布地；
    5. 如果卡片没带城市，再只针对“已确认在售”的商品，用本地发布日志/商品缓存/素材库补城市；
    6. 严格过滤 {}, [], null, 未获取 之类无效值，避免前端出现绿色 {}。
    """
    if not conversations:
        logger.info(f"【{account_id}】黄色商品标题补城市：conversations为空，跳过")
        return {}

    conv_keys: dict[str, dict[str, str]] = {}
    yellow_titles: list[str] = []
    item_ids: list[str] = []

    for c in conversations:
        title = str(c.get("itemTitle") or "").strip()[:200]
        if not title:
            continue
        item_id = str(c.get("itemId") or "").strip()
        cid = str(c.get("cid") or "").strip()
        raw_cid = str(c.get("rawCid") or "").strip()
        other_user_id = str(c.get("otherUserId") or "").strip()
        key = cid or raw_cid or other_user_id
        if not key:
            continue
        conv_keys[key] = {
            "title": title,
            "item_id": item_id,
            "cid": cid,
            "raw_cid": raw_cid,
            "other_user_id": other_user_id,
        }
        yellow_titles.append(title)
        if item_id:
            item_ids.append(item_id)

    yellow_titles = list(dict.fromkeys([v for v in yellow_titles if v]))
    item_ids = list(dict.fromkeys([v for v in item_ids if v]))

    if not yellow_titles:
        logger.info(f"【{account_id}】黄色商品标题补城市：会话列表没有 itemTitle，跳过")
        return {}

    logger.info(
        f"【{account_id}】v1.4.0 在售商品发布地匹配：titles={yellow_titles[:8]}, "
        f"itemIds={item_ids[:8]}, conversations={len(conv_keys)}"
    )

    # 先查当前卖家“在售”商品列表。只有这里匹配到的咨询商品，后面才允许显示城市标签。
    try:
        online_result = await _get_city_map_from_seller_items_by_title(
            db=db,
            current_user=current_user,
            account_id=account_id,
            titles=yellow_titles,
            item_ids=item_ids,
        )
    except Exception as e:
        logger.warning(f"【{account_id}】v1.4.0 查询当前在售商品失败: {e}")
        online_result = {}

    online_by_title = (online_result or {}).get("by_title") or {}
    online_by_item_id = (online_result or {}).get("by_item_id") or {}
    matched_titles = set(str(x) for x in ((online_result or {}).get("matched_titles") or []) if str(x or "").strip())
    matched_item_ids = set(str(x) for x in ((online_result or {}).get("matched_item_ids") or []) if str(x or "").strip())

    if not matched_titles and not matched_item_ids:
        logger.info(
            f"【{account_id}】v1.4.0 黄色商品都不在当前在售列表，跳过城市标签："
            f"titles={yellow_titles[:5]}, itemIds={item_ids[:5]}"
        )
        return {}

    title_city: dict[str, str] = {}
    item_city: dict[str, str] = {}

    def normalize_city(v: object) -> str:
        city = _extract_city_from_location_text(v) or _clean_city_label(v)
        return city if _is_valid_city_label(city) else ""

    for t, city in online_by_title.items():
        t = str(t or "").strip()[:200]
        city = normalize_city(city)
        if t and city:
            title_city.setdefault(t, city)
    for iid, city in online_by_item_id.items():
        iid = str(iid or "").strip()
        city = normalize_city(city)
        if iid and city:
            item_city.setdefault(iid, city)

    # 只针对“已确认仍在售”的商品，用本地发布日志/商品缓存/素材库补城市。
    # 这样已下架商品即使本地库有记录，也不会再显示城市标签。
    def title_allowed(db_title: object) -> list[str]:
        db_title_text = str(db_title or "").strip()[:200]
        if not db_title_text:
            return []
        return [t for t in matched_titles if db_title_text == t or _title_rough_match(t, db_title_text)]

    def put_local_title_city(src_title: object, city_value: object):
        city = normalize_city(city_value)
        if not city:
            return
        for t in title_allowed(src_title):
            title_city.setdefault(t, city)

    try:
        q = (
            select(
                PublishLog.item_id,
                PublishLog.item_url,
                PublishLog.title,
                PublishLog.resolved_address_text,
                ProductMaterial.address,
                PublishLog.created_at,
            )
            .outerjoin(ProductMaterial, ProductMaterial.id == PublishLog.material_id)
            .where(PublishLog.account_id == account_id, PublishLog.status == "success")
            .order_by(desc(PublishLog.created_at))
            .limit(2000)
        )
        rows = (await db.execute(q)).all()
        for log_item_id, item_url, db_title, resolved_address, material_address, _created_at in rows:
            city = normalize_city(resolved_address) or normalize_city(material_address)
            if not city:
                continue
            if log_item_id and str(log_item_id) in matched_item_ids:
                item_city.setdefault(str(log_item_id), city)
            if item_url:
                iid = _extract_item_id_from_any(str(item_url))
                if iid and iid in matched_item_ids:
                    item_city.setdefault(iid, city)
            put_local_title_city(db_title, city)
    except Exception as e:
        logger.warning(f"【{account_id}】v1.4.0 查询在售商品发布日志城市失败: {e}")

    try:
        account_query = select(XYAccount).where(XYAccount.account_id == account_id)
        account = (await db.execute(account_query)).scalar_one_or_none()
        if account:
            conditions = [XYCatalogItem.account_pk == account.id]
            if not is_admin_user(current_user):
                conditions.append(XYCatalogItem.owner_id == current_user.id)
            match_conditions = []
            if matched_item_ids:
                match_conditions.append(XYCatalogItem.item_id.in_(list(matched_item_ids)))
            if matched_titles:
                match_conditions.append(XYCatalogItem.title.in_(list(matched_titles)))
            if match_conditions:
                q = (
                    select(XYCatalogItem.item_id, XYCatalogItem.title, XYCatalogItem.metadata_json, XYCatalogItem.updated_at, XYCatalogItem.created_at)
                    .where(*conditions, or_(*match_conditions))
                    .order_by(desc(XYCatalogItem.updated_at), desc(XYCatalogItem.created_at))
                    .limit(500)
                )
                rows = (await db.execute(q)).all()
                for db_item_id, db_title, metadata, _updated_at, _created_at in rows:
                    city = normalize_city(_extract_city_from_item_payload(metadata or {}))
                    if not city:
                        continue
                    if db_item_id and str(db_item_id) in matched_item_ids:
                        item_city.setdefault(str(db_item_id), city)
                    put_local_title_city(db_title, city)
    except Exception as e:
        logger.warning(f"【{account_id}】v1.4.0 查询在售商品管理缓存城市失败: {e}")

    try:
        if matched_titles:
            material_conditions = [ProductMaterial.address.isnot(None), ProductMaterial.address != ""]
            if not is_admin_user(current_user):
                material_conditions.append(ProductMaterial.user_id == current_user.id)
            q = (
                select(ProductMaterial.title, ProductMaterial.address, ProductMaterial.updated_at, ProductMaterial.created_at)
                .where(*material_conditions)
                .order_by(desc(ProductMaterial.updated_at), desc(ProductMaterial.created_at))
                .limit(3000)
            )
            rows = (await db.execute(q)).all()
            for db_title, address, _updated_at, _created_at in rows:
                put_local_title_city(db_title, address)
    except Exception as e:
        logger.warning(f"【{account_id}】v1.4.0 查询在售商品素材库城市失败: {e}")

    city_map: dict[str, str] = {}
    in_stock_no_city: list[dict[str, str]] = []
    skipped_offline: list[dict[str, str]] = []
    for info in conv_keys.values():
        title = info.get("title", "")
        iid = info.get("item_id", "")
        is_online = bool((iid and iid in matched_item_ids) or (title and title in matched_titles))
        if not is_online:
            if len(skipped_offline) < 8:
                skipped_offline.append({"itemId": iid, "title": title})
            continue

        city = ""
        if iid:
            city = normalize_city(item_city.get(iid, ""))
        if not city and title:
            city = normalize_city(title_city.get(title, ""))

        if city:
            for k in [info.get("cid"), info.get("raw_cid"), info.get("other_user_id")]:
                if k:
                    city_map.setdefault(str(k), city)
        elif len(in_stock_no_city) < 8:
            in_stock_no_city.append({"itemId": iid, "title": title})

    if city_map:
        logger.info(f"【{account_id}】v1.4.0 已给当前在售咨询商品补充发布地城市：{len(city_map)} 条映射")
    if in_stock_no_city:
        logger.info(f"【{account_id}】v1.4.0 当前在售商品已匹配但没有可用城市，前端不显示标签：{in_stock_no_city}")
    if skipped_offline:
        logger.info(f"【{account_id}】v1.4.0 咨询商品不在当前在售列表，跳过城市标签：{skipped_offline}")
    return city_map


async def _get_city_map_from_seller_items_by_title(
    db: AsyncSession,
    current_user: User,
    account_id: str,
    titles: list[str],
    item_ids: list[str] | None = None,
) -> dict[str, object]:
    """v1.4.0：只拉取当前卖家“在售”商品列表。

    返回：
    - by_title / by_item_id：成功提取到城市的映射；
    - matched_titles / matched_item_ids：确认仍在“在售”列表里的咨询商品。

    注意：matched 只代表商品仍在售，不代表一定取到了城市。
    """
    clean_titles = list(dict.fromkeys([str(t or "").strip()[:200] for t in titles if str(t or "").strip()]))
    clean_item_ids = list(dict.fromkeys([str(i or "").strip() for i in (item_ids or []) if str(i or "").strip()]))
    by_title: dict[str, str] = {}
    by_item_id: dict[str, str] = {}
    matched_titles: set[str] = set()
    matched_item_ids: set[str] = set()

    if not clean_titles and not clean_item_ids:
        return {"by_title": by_title, "by_item_id": by_item_id, "matched_titles": [], "matched_item_ids": []}

    def normalize_city(v: object) -> str:
        city = _extract_city_from_location_text(v) or _clean_city_label(v)
        return city if _is_valid_city_label(city) else ""

    try:
        account_query = select(XYAccount).where(XYAccount.account_id == account_id)
        account = (await db.execute(account_query)).scalar_one_or_none()
        if not account or not account.cookie:
            logger.info(f"【{account_id}】卖家商品城市搜索跳过：账号不存在或 Cookie 为空")
            return {"by_title": by_title, "by_item_id": by_item_id, "matched_titles": [], "matched_item_ids": []}

        from common.utils.item_info_manager import ItemInfoManager

        myid = extract_account_user_id_from_cookie(account.cookie) or str(account.unb or "").strip() or str(account.account_id or "").strip()
        manager = ItemInfoManager(account.account_id, account.cookie)
        try:
            page_size_candidates = [20, 10, 5]
            loaded_any_page = False
            for page_size in page_size_candidates:
                max_pages = 6 if page_size >= 20 else 10
                for page in range(1, max_pages + 1):
                    result = await manager.get_item_list_info(page_number=page, page_size=page_size, myid=myid)
                    if not result or not result.get("success"):
                        logger.info(f"【{account_id}】卖家商品城市搜索第{page}页失败：pageSize={page_size}, result={result}")
                        break

                    loaded_any_page = True
                    items = result.get("items") or []
                    raw_cards = ((result.get("raw_data") or {}).get("cardList") or []) if isinstance(result.get("raw_data"), dict) else []
                    if not items and not raw_cards:
                        break

                    candidates: list[tuple[dict, dict]] = []
                    for item in items:
                        if isinstance(item, dict):
                            candidates.append((item, {}))
                    for card in raw_cards:
                        if not isinstance(card, dict):
                            continue
                        card_data = card.get("cardData") or {}
                        if isinstance(card_data, dict):
                            candidates.append((card_data, card))

                    page_seen: list[str] = []
                    for item, raw_card in candidates:
                        if not isinstance(item, dict):
                            continue
                        item_id = str(item.get("id") or item.get("item_id") or item.get("itemId") or "").strip()
                        title = str(item.get("title") or item.get("item_title") or item.get("itemTitle") or "").strip()[:200]
                        if item_id or title:
                            page_seen.append(f"{item_id}:{title[:30]}")
                        if not item_id and not title:
                            continue

                        hit_titles = [t for t in clean_titles if title and (title == t or _title_rough_match(t, title))]
                        hit_id = bool(item_id and item_id in clean_item_ids)
                        if not hit_titles and not hit_id:
                            continue

                        if hit_id:
                            matched_item_ids.add(item_id)
                        for t in hit_titles:
                            matched_titles.add(t)

                        city = (
                            normalize_city(_extract_city_from_item_payload(item))
                            or normalize_city(_extract_city_from_item_payload(raw_card))
                            or normalize_city(_extract_city_from_item_payload(item.get("detailParams") or {}))
                            or normalize_city(_extract_city_from_item_payload(item.get("trackParams") or {}))
                            or normalize_city(_extract_city_from_item_payload(item.get("itemLabelDataVO") or item.get("item_label_data") or {}))
                        )
                        if city:
                            logger.info(f"【{account_id}】卖家在售商品已匹配并取到城市：itemId={item_id}, title={title}, city={city}")
                            if item_id:
                                by_item_id.setdefault(item_id, city)
                            for t in hit_titles:
                                by_title.setdefault(t, city)
                        else:
                            try:
                                debug_fields = _collect_city_debug_fields({"item": item, "rawCard": raw_card}, limit=24)
                            except Exception as debug_error:
                                debug_fields = [f"debugFields收集失败: {debug_error}"]
                            logger.info(
                                f"【{account_id}】卖家在售商品已匹配但未取到城市：itemId={item_id}, title={title}, "
                                f"debugFields={debug_fields[:24]}"
                            )

                    if page_seen:
                        logger.info(f"【{account_id}】v1.4.1 当前在售商品列表第{page}页样例：{page_seen[:8]}")

                    # 所有咨询商品都已经确认是否在售后，就不用继续翻页。
                    all_ids_matched = not clean_item_ids or set(clean_item_ids).issubset(matched_item_ids)
                    all_titles_matched = not clean_titles or set(clean_titles).issubset(matched_titles)
                    if all_ids_matched and all_titles_matched:
                        break
                    if len(items) < page_size:
                        break
                    await asyncio.sleep(0.35)

                if loaded_any_page:
                    break
        finally:
            await manager.close()

        logger.info(
            f"【{account_id}】v1.4.1 当前在售匹配结果："
            f"matchedTitles={list(matched_titles)[:5]}, matchedItemIds={list(matched_item_ids)[:5]}, "
            f"cityTitles={len(by_title)}, cityItemIds={len(by_item_id)}"
        )
        return {
            "by_title": by_title,
            "by_item_id": by_item_id,
            "matched_titles": list(matched_titles),
            "matched_item_ids": list(matched_item_ids),
        }
    except Exception as e:
        logger.warning(f"【{account_id}】卖家商品城市搜索异常: {e}")
        return {"by_title": by_title, "by_item_id": by_item_id, "matched_titles": list(matched_titles), "matched_item_ids": list(matched_item_ids)}


async def _fetch_city_from_item_detail(account: XYAccount, item_id: str) -> str:
    """用商品 ID 直接查帖子城市。

    v1.2.8 修复：
    1. 既然会话列表已经解析到黄色商品对应的 itemId，就不应主要依赖卖家商品列表翻页；
    2. 优先调用 mtop.taobao.idle.pc.detail；
    3. mtop 详情无城市时，再直接请求 goofish 商品详情页 HTML/内嵌 JSON，解析“宝贝所在地/发货地/城市”等字段。
    """
    if not item_id:
        return ""
    item_id = str(item_id).strip()
    try:
        from common.services.xianyu_detail_client import XianyuItemDetailClient

        client = XianyuItemDetailClient(
            cookie_id=account.account_id,
            cookies_str=account.cookie,
            owner_id=account.owner_id,
        )
        result = await client.get_detail(item_id)
        if result and result.get("success"):
            detail = result.get("detail") or {}
            city = _extract_city_from_item_payload(detail)
            if city:
                logger.info(f"【{account.account_id}】已从商品详情接口解析城市：itemId={item_id}, city={city}")
                return city
            # 打印关键字段，方便确认发布地到底藏在哪个字段里。不要打印完整详情，避免日志过大。
            city_key_samples: list[str] = []
            try:
                detail_text = json.dumps(detail, ensure_ascii=False)[:20000] if isinstance(detail, dict) else str(detail)[:20000]
                for m in re.finditer(r'"([^"\]*(?:city|City|location|Location|area|Area|province|Province|address|Address|district|District|publish|Publish|所在地|发货地|发布地)[^"\]*)"\s*:\s*"([^"\]{0,60})"', detail_text):
                    city_key_samples.append(f"{m.group(1)}={m.group(2)}")
                    if len(city_key_samples) >= 8:
                        break
            except Exception:
                city_key_samples = []
            logger.info(
                f"【{account.account_id}】商品详情接口成功但未解析到城市："
                f"itemId={item_id}, keys={list(detail.keys())[:20] if isinstance(detail, dict) else type(detail).__name__}, "
                f"cityFields={city_key_samples}"
            )
        else:
            logger.info(
                f"【{account.account_id}】商品详情接口未成功：itemId={item_id}, "
                f"error={(result or {}).get('error') if isinstance(result, dict) else result}"
            )
    except Exception as e:
        logger.info(f"【{account.account_id}】商品详情接口城市兜底失败 itemId={item_id}: {e}")

    # mtop 详情接口拿不到城市时，直接访问商品详情页。你在网页上能看到城市，通常就在这里的 HTML/JSON 里。
    return await _fetch_city_from_item_web_page(account, item_id)


async def _fetch_city_from_item_web_page(account: XYAccount, item_id: str) -> str:
    """直接请求商品详情页，解析页面里的发布城市/宝贝所在地。"""
    if not item_id:
        return ""
    try:
        import aiohttp
        import html as html_lib

        cookie = account.cookie or ""
        headers = {
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.goofish.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        urls = [
            f"https://www.goofish.com/item?id={item_id}",
            f"https://www.goofish.com/item.htm?id={item_id}",
        ]
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
            for url in urls:
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True) as resp:
                        text = await resp.text(errors="ignore")
                        if resp.status >= 400 or not text:
                            logger.info(f"【{account.account_id}】商品详情页请求失败：itemId={item_id}, status={resp.status}, url={url}")
                            continue
                        city = _extract_city_from_item_page_text(text)
                        if city:
                            logger.info(f"【{account.account_id}】已从商品详情页解析城市：itemId={item_id}, city={city}")
                            return city
                        logger.info(f"【{account.account_id}】商品详情页未解析到城市：itemId={item_id}, status={resp.status}, url={url}, len={len(text)}")
                except Exception as exc:
                    logger.info(f"【{account.account_id}】商品详情页请求异常：itemId={item_id}, url={url}, error={exc}")
        # v1.3.4：不要在会话列表接口里启动 Playwright 渲染商品页。
        # Playwright 对多会话/多 itemId 会把在线聊天列表请求拖到 90 秒超时。
        # 这里先返回空，保证会话列表正常打开；后续如果继续做城市，必须改成后台异步任务。
        logger.info(f"【{account.account_id}】商品详情页未取得城市，已跳过渲染兜底避免阻塞会话列表：itemId={item_id}")
        return ""
    except Exception as e:
        logger.info(f"【{account.account_id}】商品详情页城市兜底失败 itemId={item_id}: {e}")
        return ""


# v1.3.5：商品发布地后台解析缓存。
# 注意：不能在会话列表接口里同步启动浏览器渲染，否则多个会话会把请求拖到 90 秒超时。
_ITEM_CITY_CACHE_PREFIX = "chat:item_city:"
_ITEM_CITY_CACHE_TTL = 7 * 24 * 3600
_ITEM_CITY_NEGATIVE_TTL = 30 * 60
_ITEM_CITY_EMPTY_VALUE = "__EMPTY__"
_ITEM_CITY_IN_PROGRESS: set[str] = set()
_ITEM_CITY_TASK_LOCK = asyncio.Lock()
_ITEM_CITY_RENDER_SEMAPHORE = asyncio.Semaphore(1)


def _item_city_cache_key(account_id: str, item_id: str) -> str:
    return f"{_ITEM_CITY_CACHE_PREFIX}{account_id}:{item_id}"


async def _read_rendered_item_city_cache(account_id: str, item_id: str) -> tuple[str, bool]:
    """读取后台渲染出来的商品发布地缓存。返回 (city, cache_hit)。

    cache_hit=True 且 city="" 表示近期已经解析失败过，先不重复投递后台任务。
    """
    item_id = str(item_id or "").strip()
    if not item_id:
        return "", True

    mem_key = f"{account_id}:{item_id}"
    if mem_key in _RENDERED_ITEM_CITY_CACHE:
        return _RENDERED_ITEM_CITY_CACHE.get(mem_key, "") or "", True

    try:
        redis_client = await get_redis_client()
        raw = await redis_client.get(_item_city_cache_key(account_id, item_id))
        if raw is None:
            return "", False
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        value = str(raw or "")
        if value == _ITEM_CITY_EMPTY_VALUE:
            _RENDERED_ITEM_CITY_CACHE[mem_key] = ""
            return "", True
        city = _extract_city_from_location_text(value) or _clean_city_label(value)
        if city:
            _RENDERED_ITEM_CITY_CACHE[mem_key] = city
            return city, True
        return "", True
    except Exception as e:
        logger.info(f"【{account_id}】读取商品发布地后台缓存失败：itemId={item_id}, error={e}")
        return "", False


async def _write_rendered_item_city_cache(account_id: str, item_id: str, city: str) -> None:
    item_id = str(item_id or "").strip()
    if not item_id:
        return
    mem_key = f"{account_id}:{item_id}"
    city = _extract_city_from_location_text(city) or _clean_city_label(city)
    _RENDERED_ITEM_CITY_CACHE[mem_key] = city or ""
    try:
        redis_client = await get_redis_client()
        if city:
            await redis_client.setex(_item_city_cache_key(account_id, item_id), _ITEM_CITY_CACHE_TTL, city)
        else:
            await redis_client.setex(_item_city_cache_key(account_id, item_id), _ITEM_CITY_NEGATIVE_TTL, _ITEM_CITY_EMPTY_VALUE)
    except Exception as e:
        logger.info(f"【{account_id}】写入商品发布地后台缓存失败：itemId={item_id}, error={e}")


async def _schedule_rendered_item_city_resolve(account: XYAccount, item_id: str, title: str = "") -> bool:
    """投递商品详情页发布地后台解析任务。立即返回，不阻塞会话列表。"""
    item_id = str(item_id or "").strip()
    if not item_id:
        return False

    task_key = f"{account.account_id}:{item_id}"
    async with _ITEM_CITY_TASK_LOCK:
        if task_key in _ITEM_CITY_IN_PROGRESS:
            return False
        _ITEM_CITY_IN_PROGRESS.add(task_key)

    account_snapshot = SimpleNamespace(
        account_id=account.account_id,
        cookie=account.cookie or "",
        owner_id=account.owner_id,
    )
    asyncio.create_task(_resolve_rendered_item_city_task(account_snapshot, item_id, title))
    return True


async def _resolve_rendered_item_city_task(account: SimpleNamespace, item_id: str, title: str = "") -> None:
    task_key = f"{account.account_id}:{item_id}"
    try:
        async with _ITEM_CITY_RENDER_SEMAPHORE:
            logger.info(f"【{account.account_id}】后台开始渲染商品详情页解析发布地：itemId={item_id}, title={str(title or '')[:80]}")
            city = await _fetch_city_from_item_rendered_page(account, item_id)
            await _write_rendered_item_city_cache(account.account_id, item_id, city)
            if city:
                logger.info(f"【{account.account_id}】后台已解析商品详情页发布地：itemId={item_id}, city={city}")
            else:
                logger.info(f"【{account.account_id}】后台未解析到商品详情页发布地：itemId={item_id}，30分钟内不重复解析")
    except Exception as e:
        logger.info(f"【{account.account_id}】后台解析商品详情页发布地异常：itemId={item_id}, error={e}")
        try:
            await _write_rendered_item_city_cache(account.account_id, item_id, "")
        except Exception:
            pass
    finally:
        async with _ITEM_CITY_TASK_LOCK:
            _ITEM_CITY_IN_PROGRESS.discard(task_key)


_RENDERED_ITEM_CITY_CACHE: dict[str, str] = {}
_RENDERED_ITEM_CITY_LOCK = asyncio.Lock()


def _cookies_for_playwright(cookie_value: str) -> list[dict]:
    """把账号 Cookie 字符串转换成 Playwright 可注入 Cookie。"""
    cookies: list[dict] = []
    seen: set[tuple[str, str]] = set()
    cookie_value = cookie_value or ""
    for pair in cookie_value.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        for domain in [".goofish.com", "www.goofish.com", ".taobao.com", ".alibaba.com"]:
            key = (name, domain)
            if key in seen:
                continue
            seen.add(key)
            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
            })
    return cookies


async def _fetch_city_from_item_rendered_page(account: XYAccount, item_id: str) -> str:
    """用真实浏览器渲染商品详情页，读取页面可见文本里的卖家资料行城市。

    你截图里的城市显示在商品详情页卖家昵称下方：
    “亳州 | 1天前来过 | 来闲鱼6年 | 卖出15件宝贝 | 好评率100%”。
    普通 aiohttp 请求经常拿到 len≈10574 的空壳页，看不到这行，所以这里用 Playwright
    等待前端接口渲染后读取 body.innerText。
    """
    if not item_id:
        return ""
    item_id = str(item_id).strip()
    cache_key = f"{account.account_id}:{item_id}"
    if cache_key in _RENDERED_ITEM_CITY_CACHE:
        return _RENDERED_ITEM_CITY_CACHE.get(cache_key, "")

    # 避免一次刷新会话时并发启动很多 Chromium。
    async with _RENDERED_ITEM_CITY_LOCK:
        if cache_key in _RENDERED_ITEM_CITY_CACHE:
            return _RENDERED_ITEM_CITY_CACHE.get(cache_key, "")

        playwright = None
        browser = None
        context = None
        try:
            from playwright.async_api import async_playwright
            from common.utils.browser_utils import ensure_playwright_browser_path, get_chromium_executable_path

            ensure_playwright_browser_path()
            playwright = await async_playwright().start()
            launch_kwargs = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-first-run",
                    "--disable-extensions",
                    "--lang=zh-CN",
                ],
            }
            chromium_path = get_chromium_executable_path()
            if chromium_path:
                launch_kwargs["executable_path"] = chromium_path

            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="zh-CN",
                viewport={"width": 1365, "height": 900},
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": "https://www.goofish.com/",
                },
            )

            cookies = _cookies_for_playwright(account.cookie or "")
            if cookies:
                await context.add_cookies(cookies)

            page = await context.new_page()
            url = f"https://www.goofish.com/item?id={item_id}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 等待页面 JS 加载商品详情。这里不要求 networkidle，因为闲鱼页面可能长期保持请求。
            try:
                await page.wait_for_function(
                    """() => {
                        const t = document.body && document.body.innerText || '';
                        return /来闲鱼|卖出\d+件宝贝|好评率|我想要|超赞|发布/.test(t) && t.length > 500;
                    }""",
                    timeout=12000,
                )
            except Exception:
                await page.wait_for_timeout(3500)

            visible_text = ""
            try:
                visible_text = await page.locator("body").inner_text(timeout=8000)
            except Exception:
                visible_text = await page.content()

            city = _extract_city_from_item_page_text(visible_text)
            if city:
                logger.info(f"【{account.account_id}】已从渲染商品详情页解析城市：itemId={item_id}, city={city}, url={page.url}")
                _RENDERED_ITEM_CITY_CACHE[cache_key] = city
                return city

            # 打一小段包含“来闲鱼”的可见文本，方便继续定位，而不是打印完整页面。
            sample = ""
            try:
                compact = re.sub(r"\s+", " ", visible_text or "")
                idx = compact.find("来闲鱼")
                if idx >= 0:
                    sample = compact[max(0, idx - 60): idx + 100]
                else:
                    sample = compact[:220]
            except Exception:
                sample = ""
            logger.info(
                f"【{account.account_id}】渲染商品详情页未解析到城市："
                f"itemId={item_id}, url={getattr(page, 'url', url)}, textLen={len(visible_text or '')}, sample={sample}"
            )
            _RENDERED_ITEM_CITY_CACHE[cache_key] = ""
            return ""
        except Exception as e:
            logger.info(f"【{account.account_id}】渲染商品详情页城市解析失败：itemId={item_id}, error={e}")
            _RENDERED_ITEM_CITY_CACHE[cache_key] = ""
            return ""
        finally:
            try:
                if context:
                    await context.close()
            except Exception:
                pass
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            try:
                if playwright:
                    await playwright.stop()
            except Exception:
                pass


def _extract_city_from_item_page_text(text: str) -> str:
    """从详情页 HTML/内嵌 JSON 文本中解析城市。"""
    if not text:
        return ""
    try:
        import html as html_lib

        raw = html_lib.unescape(str(text))
        raw = unquote(raw)
        # 去掉 HTML 标签后保留一份纯文本，适合 “宝贝所在地 山东烟台” 这种页面文案。
        plain = re.sub(r"<[^>]+>", " ", raw)
        plain = re.sub(r"\s+", " ", plain)

        # 先解析明确中文标签附近的城市。闲鱼商品页可能显示“宝贝所在地 / 发货地 / 发布地 / 所在地”。
        for pattern in [
            r"(?:宝贝所在地|发货地|所在地|发布于|商品所在地|发布城市|发布地|城市|地区)[:：]?\s*([^,，。；;|<>\s]{2,30})",
            r"(?:宝贝所在地|发货地|所在地|发布于|商品所在地|发布城市|发布地|城市|地区).{0,40}?([北京上海天津重庆香港澳门]|(?:广东|浙江|江苏|山东|河南|河北|湖南|湖北|福建|四川|安徽|江西|辽宁|吉林|黑龙江|陕西|山西|云南|贵州|海南|甘肃|青海|台湾|广西|宁夏|新疆|西藏|内蒙古)[^,，。；;|<>\s]{1,12})",
            r"([北京上海天津重庆香港澳门]|(?:广东|浙江|江苏|山东|河南|河北|湖南|湖北|福建|四川|安徽|江西|辽宁|吉林|黑龙江|陕西|山西|云南|贵州|海南|甘肃|青海|台湾|广西|宁夏|新疆|西藏|内蒙古)[^,，。；;|<>\s]{1,12})(?:发货|发布|所在地)",
        ]:
            for m in re.finditer(pattern, plain):
                city = _extract_city_from_location_text(m.group(1))
                if city:
                    return city

        # v1.3.1：解析你截图里这种卖家资料行：
        # “亳州 | 1天前来过 | 来闲鱼6年 | 卖出15件宝贝 | 好评率100%”。
        # 这里的第一个短中文词就是商品详情页显示的发布/所在地。
        seller_profile_city_patterns = [
            # 页面真实文本可能包含可见竖线：
            # “亳州 | 1天前来过 | 来闲鱼6年 | 卖出15件宝贝”
            r"(?:^|\s)([\u4e00-\u9fa5]{2,8})\s*[|｜]\s*\d+\s*(?:分钟|小时|天|月|年)前来过\s*[|｜]\s*来闲鱼",
            r"(?:^|\s)([\u4e00-\u9fa5]{2,8})\s*[|｜]\s*来闲鱼\d+年",
            r"(?:^|\s)([\u4e00-\u9fa5]{2,8})\s*[|｜]\s*(?:卖出\d+件宝贝|好评率\d+%)",
            # 也可能 DOM 里竖线只是 CSS 分隔符，innerText 变成：
            # “亳州 1天前来过 来闲鱼6年 卖出15件宝贝”
            r"(?:^|\s)([\u4e00-\u9fa5]{2,8})\s+\d+\s*(?:分钟|小时|天|月|年)前来过\s+来闲鱼",
            r"(?:^|\s)([\u4e00-\u9fa5]{2,8})\s+来闲鱼\d+年",
            r"(?:^|\s)([\u4e00-\u9fa5]{2,8})\s+(?:卖出\d+件宝贝|好评率\d+%)",
            # 多行文本被压缩后也可能是：
            # “亳州 1天前来过” 后面未必紧跟来闲鱼，先把第一个短中文词取出。
            r"(?:^|\s)([\u4e00-\u9fa5]{2,8})\s+\d+\s*(?:分钟|小时|天|月|年)前来过",
        ]
        invalid_profile_city_words = {
            "来闲鱼", "卖出", "好评率", "小时前", "分钟前", "天前", "月前", "年前",
            "全新", "包邮", "宝贝", "设备", "电源", "标题", "详情",
        }
        for pattern in seller_profile_city_patterns:
            for m in re.finditer(pattern, plain):
                candidate = str(m.group(1) or "").strip()
                if candidate in invalid_profile_city_words:
                    continue
                city = _extract_city_from_location_text(candidate)
                if city:
                    return city

        # 再解析 JSON key/value。
        key_patterns = [
            "city", "cityName", "city_name", "itemCity", "item_city",
            "location", "locationName", "itemLocation", "item_location",
            "area", "areaName", "region", "regionName", "province", "provinceName",
            "address", "itemAddress", "sellerAddress", "poiName",
        ]
        joined_keys = "|".join(re.escape(k) for k in key_patterns)
        for m in re.finditer(rf'"(?:{joined_keys})"\s*:\s*"([^"]{{1,40}})"', raw, flags=re.I):
            city = _extract_city_from_location_text(m.group(1))
            if city:
                return city
        for m in re.finditer(rf"'(?:{joined_keys})'\s*:\s*'([^']{{1,40}})'", raw, flags=re.I):
            city = _extract_city_from_location_text(m.group(1))
            if city:
                return city

        return ""
    except Exception:
        return ""




async def _get_buyer_city_map_from_orders(
    db: AsyncSession,
    current_user: User,
    account_id: str,
    conversations: list[dict],
) -> dict[str, str]:
    """从 xy_orders.receiver_address 中提取买家城市。返回 buyer_id/cid -> city 映射。"""
    if not conversations:
        return {}

    buyer_ids = [str(c.get("otherUserId") or "").strip() for c in conversations if c.get("otherUserId")]
    cids = [str(c.get("cid") or "").strip() for c in conversations if c.get("cid")]
    raw_cids = [str(c.get("rawCid") or "").strip() for c in conversations if c.get("rawCid")]
    buyer_ids = [v for v in buyer_ids if v]
    all_cids = [v for v in set(cids + raw_cids) if v]
    if not buyer_ids and not all_cids:
        return {}

    where_clauses = [XYOrder.account_id == account_id, XYOrder.receiver_address.isnot(None), XYOrder.receiver_address != ""]
    if not is_admin_user(current_user):
        where_clauses.append(XYOrder.owner_id == current_user.id)

    match_clauses = []
    if buyer_ids:
        match_clauses.append(XYOrder.buyer_id.in_(buyer_ids))
    if all_cids:
        match_clauses.append(XYOrder.chat_id.in_(all_cids))
    if not match_clauses:
        return {}

    q = (
        select(XYOrder.buyer_id, XYOrder.chat_id, XYOrder.receiver_address, XYOrder.created_at, XYOrder.placed_at)
        .where(*where_clauses, or_(*match_clauses))
        .order_by(desc(XYOrder.placed_at), desc(XYOrder.created_at))
        .limit(300)
    )
    rows = (await db.execute(q)).all()
    city_map: dict[str, str] = {}
    for buyer_id, chat_id, receiver_address, _created_at, _placed_at in rows:
        city = _extract_city_from_receiver_address(receiver_address)
        if not city:
            continue
        if buyer_id:
            city_map.setdefault(str(buyer_id), city)
        if chat_id:
            city_map.setdefault(str(chat_id), city)
    if city_map:
        logger.info(f"【{account_id}】已从订单收货地址补充买家城市：{len(city_map)} 条映射")
    return city_map


def _extract_city_from_receiver_address(address: object) -> str:
    """从中文收货地址中提取短城市标签。"""
    if address is None:
        return ""
    text = str(address).strip()
    if not text:
        return ""
    # 去掉常见脱敏/分隔符导致的干扰
    text = re.sub(r"\s+", "", text)
    text = text.replace("中国", "")

    # 直辖市/特别行政区：北京市朝阳区 -> 北京
    for name in ["北京", "上海", "天津", "重庆", "香港", "澳门"]:
        if text.startswith(name):
            return name
        if text.startswith(name + "市"):
            return name

    # 自治区：广西壮族自治区南宁市 -> 南宁；内蒙古自治区呼和浩特市 -> 呼和浩特
    autonomous_patterns = [
        r"^(?:广西壮族自治区|广西)([^省市区县]{2,8}?市)",
        r"^(?:宁夏回族自治区|宁夏)([^省市区县]{2,8}?市)",
        r"^(?:新疆维吾尔自治区|新疆)([^省市区县]{2,8}?市)",
        r"^(?:西藏自治区|西藏)([^省市区县]{2,8}?市)",
        r"^(?:内蒙古自治区|内蒙古)([^省市区县]{2,8}?市)",
    ]
    for pattern in autonomous_patterns:
        m = re.search(pattern, text)
        if m:
            return _clean_city_label(m.group(1))

    # 普通省份：浙江省杭州市 / 广东深圳市 / 江苏省苏州市
    m = re.search(r"^(?:[^省]{2,8}省)?([^省市区县]{2,10}?市)", text)
    if m:
        return _clean_city_label(m.group(1))

    # 如果没有“市”，尝试地区/州/盟：吉林省延边朝鲜族自治州 -> 延边
    m = re.search(r"^(?:[^省]{2,8}省)?([^省市区县]{2,10}?(?:地区|自治州|州|盟))", text)
    if m:
        return _clean_city_label(m.group(1))

    # 最后兜底：省份标签
    m = re.search(r"^([^省市区县]{2,8}?省)", text)
    if m:
        return _clean_city_label(m.group(1))
    return ""



def _is_valid_city_label(value: object) -> bool:
    """过滤无效城市标签，避免前端显示绿色 {} / [] / null。"""
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    bad_values = {"{}", "[]", "null", "None", "none", "undefined", "未知", "未获取", "中国", "全国", "-", "--"}
    if text in bad_values:
        return False
    # JSON/对象/数组字符串不是城市。
    if text.startswith(("{", "[", "(")) or text.endswith(("}", "]", ")")):
        return False
    # 只有标点符号也不是城市。
    if not re.search(r"[\u4e00-\u9fa5]", text):
        return False
    if len(text) > 12:
        return False
    return True

def _clean_city_label(value: object) -> str:
    """将接口返回的地址/城市字段清洗成短城市标签。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not _is_valid_city_label(text):
        return ""
    # 避免把详细地址展示出来，只取较短的地区片段
    for sep in ["/", "|", ",", "，", " "]:
        if sep in text:
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            # 倾向取最后一级城市/地区
            text = parts[-1] if parts else text
            break
    for prefix in ["中国", "中华人民共和国"]:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    # 太长的值可能是详细地址，截短保护页面布局和隐私
    if len(text) > 12:
        text = text[:12]
    return text



def _extract_item_id_from_any(data: object) -> str:
    """从会话 extension / 消息卡片中尽量提取咨询商品 ID。"""
    if data is None:
        return ""
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return ""
        # 字符串可能是 JSON，也可能是链接/卡片文本。
        try:
            parsed = json.loads(text)
            item_id = _extract_item_id_from_any(parsed)
            if item_id:
                return item_id
        except (json.JSONDecodeError, TypeError):
            pass
        patterns = [
            r"(?:itemId|item_id|itemid|itemID|item_id_str|fishItemId|fisItemId)[\"'\s:=]+([0-9]{6,})",
            r"(?:id=|id%3D)([0-9]{6,})",
            r"/item[/?:][^0-9]*([0-9]{6,})",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1)
        return ""
    if isinstance(data, list):
        for item in data:
            item_id = _extract_item_id_from_any(item)
            if item_id:
                return item_id
        return ""
    if not isinstance(data, dict):
        return ""

    # 先找常见明确字段，避免把用户 ID 误判成商品 ID。
    for key in [
        "itemId", "item_id", "itemid", "itemID", "item_id_str",
        "fishItemId", "fisItemId", "auctionId", "auction_id",
        "goodsId", "goods_id", "commodityId", "commodity_id",
    ]:
        val = data.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if re.fullmatch(r"[0-9]{6,}", text):
            return text

    # 再递归常见嵌套对象/字符串。
    for key in [
        "item", "itemInfo", "itemCard", "card", "goods", "commodity",
        "extension", "ext", "params", "data", "extra", "template", "targetUrl", "url",
    ]:
        if key in data:
            item_id = _extract_item_id_from_any(data.get(key))
            if item_id:
                return item_id

    return ""


def _extract_item_title_from_any(data: object) -> str:
    """从会话/消息卡片任意层级尽量提取咨询商品标题。"""
    if data is None:
        return ""
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return ""
        # 有些字段是 JSON 字符串或 base64 后的 JSON 字符串
        try:
            parsed = json.loads(text)
            title = _extract_item_title_from_any(parsed)
            if title:
                return title
        except Exception:
            pass
        # 避免把普通聊天内容当商品标题；只处理明显的卡片 JSON/摘要。
        m = re.search(r'(?:itemTitle|item_title|goodsTitle|title)["\'\s:=：]+([^"\'{}]{4,200})', text, re.I)
        if m:
            return str(m.group(1)).strip()[:200]
        return ""
    if isinstance(data, list):
        for item in data:
            title = _extract_item_title_from_any(item)
            if title:
                return title
        return ""
    if isinstance(data, dict):
        title_keys = [
            "itemTitle", "item_title", "itemName", "item_name", "fishItemTitle",
            "goodsTitle", "goods_title", "commodityTitle", "commodity_title",
            "mainTitle", "subject", "title", "name",
        ]
        for key in title_keys:
            value = data.get(key)
            if isinstance(value, str):
                cleaned = value.strip()
                # 过滤太短/系统卡片词
                if len(cleaned) >= 4 and cleaned not in {"[卡片消息]", "卡片消息", "系统消息"}:
                    return cleaned[:200]
        nested_keys = [
            "item", "itemInfo", "itemCard", "card", "goods", "commodity",
            "data", "body", "content", "custom", "template", "extension",
        ]
        for key in nested_keys:
            if key in data:
                title = _extract_item_title_from_any(data.get(key))
                if title:
                    return title
        for value in data.values():
            title = _extract_item_title_from_any(value)
            if title:
                return title
    return ""


def _normalize_title_for_match(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\\s+", "", text)
    text = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", text)
    return text[:180]


def _title_rough_match(a: object, b: object) -> bool:
    na = _normalize_title_for_match(a)
    nb = _normalize_title_for_match(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # 闲鱼会话里可能只给标题前半段，允许较长片段互相包含。
    if len(na) >= 8 and na in nb:
        return True
    if len(nb) >= 8 and nb in na:
        return True
    return False




def _collect_city_debug_fields(data: object, limit: int = 60) -> list[str]:
    """收集商品卡片里可能和发布地有关的字段，便于定位闲鱼真实返回结构。

    v1.4.1：修复“位置”的错误 Unicode 正则，避免 incomplete escape 崩溃。
    """
    results: list[str] = []
    seen: set[int] = set()
    city_hint = re.compile(r"city|location|loc|area|region|province|address|poi|place|publish|district|zone|position|addr|所在地|发布|发货|地区|城市|位置|地址", re.I)
    text_hint = re.compile(r"\u5317\u4eac|\u4e0a\u6d77|\u5929\u6d25|\u91cd\u5e86|\u9999\u6e2f|\u6fb3\u95e8|\u5e7f\u4e1c|\u6d59\u6c5f|\u6c5f\u82cf|\u5c71\u4e1c|\u6cb3\u5357|\u6cb3\u5317|\u6e56\u5357|\u6e56\u5317|\u798f\u5efa|\u56db\u5ddd|\u5b89\u5fbd|\u6c5f\u897f|\u8fbd\u5b81|\u5409\u6797|\u9ed1\u9f99\u6c5f|\u9655\u897f|\u5c71\u897f|\u4e91\u5357|\u8d35\u5dde|\u6d77\u5357|\u7518\u8083|\u9752\u6d77|\u53f0\u6e7e|\u5e7f\u897f|\u5b81\u590f|\u65b0\u7586|\u897f\u85cf|\u5185\u8499\u53e4")

    def add(path: str, value: object):
        if len(results) >= limit:
            return
        text = str(value).strip()
        if not text:
            return
        text = re.sub(r"\s+", " ", text)
        if len(text) > 180:
            text = text[:180] + "..."
        results.append(f"{path}={text}")

    def walk(obj: object, path: str = "$", depth: int = 0):
        if obj is None or depth > 8 or len(results) >= limit:
            return
        oid = id(obj)
        if isinstance(obj, (dict, list)):
            if oid in seen:
                return
            seen.add(oid)
        if isinstance(obj, str):
            text = obj.strip()
            if not text:
                return
            # JSON 字符串继续展开
            if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
                try:
                    walk(json.loads(text), path, depth + 1)
                    return
                except Exception:
                    pass
            if city_hint.search(path) or text_hint.search(text):
                add(path, text)
            return
        if isinstance(obj, list):
            for i, v in enumerate(obj[:80]):
                walk(v, f"{path}[{i}]", depth + 1)
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kp = f"{path}.{k}"
                if city_hint.search(str(k)):
                    add(kp, v)
                walk(v, kp, depth + 1)
    walk(data)
    return results[:limit]


def _extract_city_from_label_like_payload(data: object) -> str:
    """从商品列表卡片里的标签数组提取发布地。闲鱼列表有时把发布地放在 label/text/title/value 里。"""
    seen: set[int] = set()
    label_keys = {"text", "title", "label", "labelText", "label_text", "name", "value", "desc", "content", "tag", "tagText"}

    def as_text(v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return ""
        return str(v).strip()

    def parse_text(text: str) -> str:
        text = re.sub(r"\s+", "", text or "")
        if not text:
            return ""
        # 明确带发布地/所在地/发货地的标签优先。
        m = re.search(r"(?:宝贝所在地|所在地|发布地|发货地|地区|位置|城市)[:：]?([^|，,;；\s]{2,12})", text)
        if m:
            return _extract_city_from_location_text(m.group(1))
        # 列表卡片常见短标签：云南 / 广东深圳 / 杭州。过滤明显非城市标签。
        bad_words = ["全新", "闲置", "卖家", "信用", "优秀", "功能", "完好", "无维修", "包邮", "想要", "人想要", "浏览", "发布", "已售", "降价", "转卖", "官方"]
        if any(w in text for w in bad_words):
            return ""
        return _extract_city_from_location_text(text)

    def walk(obj: object, parent_key: str = "", depth: int = 0) -> str:
        if obj is None or depth > 10:
            return ""
        oid = id(obj)
        if isinstance(obj, (dict, list)):
            if oid in seen:
                return ""
            seen.add(oid)
        if isinstance(obj, str):
            if (obj.strip().startswith("{") and obj.strip().endswith("}")) or (obj.strip().startswith("[") and obj.strip().endswith("]")):
                try:
                    city = walk(json.loads(obj), parent_key, depth + 1)
                    if city:
                        return city
                except Exception:
                    pass
            # 只有 label/tag/location 相关父级才直接从纯文本短标签猜。
            if re.search(r"label|tag|city|location|area|region|address|place|\u6240\u5728|\u53d1\u5e03|\u53d1\u8d27|\u5730\u533a", parent_key, re.I):
                return parse_text(obj)
            return ""
        if isinstance(obj, list):
            # 标签列表中的短文本字段逐个解析。
            for it in obj:
                city = walk(it, parent_key, depth + 1)
                if city:
                    return city
            return ""
        if isinstance(obj, dict):
            # text/value 同时存在于 label/tag 类对象时优先解析。
            joined = "".join(as_text(obj.get(k)) for k in label_keys if k in obj)
            if joined:
                city = parse_text(joined)
                if city:
                    return city
            for k, v in obj.items():
                city = walk(v, str(k), depth + 1)
                if city:
                    return city
        return ""
    return walk(data)

def _extract_city_from_item_payload(data: object) -> str:
    """从商品列表卡片/商品详情/本地 metadata 中尽量提取“宝贝所在地”。

    v1.2.9 修复点：
    闲鱼商品详情接口里，发布地经常不在顶层 city/location 字段，
    而是藏在 itemDO、apiStack.value、trackParams、detailParams、cardData 等深层 JSON 字符串里。
    旧版只递归少数字段，导致“页面明明有发布地，但程序取不到”。

    这里改成：
    1. 仍然只从 city/location/area/address/province 等地区含义字段取值，避免从标题/描述乱猜；
    2. 但会递归扫描所有 dict/list，并解析内嵌 JSON 字符串；
    3. 同时兼容常见 key：publishArea、publishLocation、sellerLocation、districtName 等。
    """
    seen: set[int] = set()
    city_key_hints = [
        "city", "location", "loc", "area", "region", "province", "address", "poi", "place",
        "publish", "district", "zone", "position", "addr",
        "所在地", "发货地", "发布地", "城市", "地区",
    ]
    strong_value_keys = [
        "city", "cityName", "city_name", "itemCity", "item_city",
        "location", "locationName", "itemLocation", "item_location",
        "area", "areaName", "area_name", "region", "regionName",
        "province", "provinceName", "district", "districtName",
        "address", "itemAddress", "sellerAddress", "poiName",
        "publishArea", "publish_area", "publishLocation", "publish_location",
        "sellerLocation", "seller_location", "sendCity", "send_city",
        "fromCity", "from_city", "itemPlace", "item_place",
    ]

    def key_has_city_hint(key: object) -> bool:
        k = str(key or "").lower()
        return any(h.lower() in k for h in city_key_hints)

    def walk(obj: object, parent_key: str = "", depth: int = 0) -> str:
        if obj is None or depth > 14:
            return ""
        obj_id = id(obj)
        if obj_id in seen:
            return ""
        if isinstance(obj, (dict, list)):
            seen.add(obj_id)

        if isinstance(obj, str):
            text = obj.strip()
            if not text:
                return ""
            # 很多接口把完整商品详情塞在 apiStack.value / xxxParams 这种 JSON 字符串里。
            if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
                try:
                    city = walk(json.loads(text), parent_key, depth + 1)
                    if city:
                        return city
                except Exception:
                    pass
            # 有些 JSON 字符串被转义后不是标准 JSON 开头，先走页面文本解析兜底。
            if key_has_city_hint(parent_key):
                city = _extract_city_from_location_text(text)
                if city:
                    return city
            return ""

        if isinstance(obj, list):
            for item in obj:
                city = walk(item, parent_key, depth + 1)
                if city:
                    return city
            return ""

        if not isinstance(obj, dict):
            return ""

        # 省+市同时存在时优先市/区县字段，再兜底省。
        for key in strong_value_keys:
            if key in obj:
                city = _extract_city_from_location_text(obj.get(key))
                if city:
                    return city

        # 先扫常见深层结构，尤其是 apiStack.value。
        priority_nested_keys = [
            "itemDO", "idleItemDO", "itemInfo", "item", "itemBaseInfo", "itemDetail",
            "apiStack", "value", "cardData", "detailParams", "trackParams",
            "itemLabelData", "itemLabelDataVO", "sellerDO", "sellerInfo",
            "publishInfo", "baseInfo", "data", "model", "props", "fields",
            "ext", "extension", "params", "extra", "renderData", "modules",
        ]
        for key in priority_nested_keys:
            if key in obj:
                city = walk(obj.get(key), key, depth + 1)
                if city:
                    return city

        # 再递归所有字段，但最终仍只会从地区含义 key 或内嵌 JSON 中返回城市。
        for key, value in obj.items():
            if key in priority_nested_keys or key in strong_value_keys:
                continue
            city = walk(value, str(key), depth + 1)
            if city:
                return city
        return ""

    city = walk(data)
    if city:
        return city
    return _extract_city_from_label_like_payload(data)


def _extract_city_from_location_text(value: object) -> str:
    """从短地区文本中提取城市标签，例如 广东深圳 / 广东省深圳市 / 发布于上海。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not _is_valid_city_label(text):
        return ""
    text = re.sub(r"\s+", "", text)
    text = text.replace("宝贝所在地", "").replace("发货地", "").replace("所在地", "")
    text = re.sub(r"^(发布于|来自|地区|位置|地址|城市|city|location)[:：]", "", text, flags=re.I)
    # 先用地址解析器，适合 广东省深圳市 / 上海市浦东新区。
    city = _extract_city_from_receiver_address(text)
    if city:
        return city
    for name in ["北京", "上海", "天津", "重庆", "香港", "澳门"]:
        if name in text:
            return name
    # 广东深圳 / 浙江杭州 / 江苏苏州 这种没有“市”的发布地文本。
    m = re.search(r"(?:广东|浙江|江苏|山东|河南|河北|湖南|湖北|福建|四川|安徽|江西|辽宁|吉林|黑龙江|陕西|山西|云南|贵州|海南|甘肃|青海|台湾|广西|宁夏|新疆|西藏|内蒙古)([^省市区县]{2,8})", text)
    if m:
        return _clean_city_label(m.group(1))
    # 如果本身就是很短的城市名/地区名，直接展示。
    if 2 <= len(text) <= 8 and not re.search(r"[0-9a-zA-Z]", text) and _is_valid_city_label(text):
        return _clean_city_label(text)
    return ""



def _extract_city_from_any(data: object) -> str:
    """从闲鱼返回的用户/会话数据里尽量提取城市字段。"""
    if not isinstance(data, dict):
        return ""

    city_keys = [
        "city", "cityName", "city_name", "location", "locationName",
        "area", "areaName", "province", "provinceName", "ipLocation",
        "userLocation", "userCity", "residence", "liveCity", "region",
    ]

    # 省+市字段同时存在时优先使用市；只有省时显示省级标签
    for key in ["city", "cityName", "city_name", "userCity", "liveCity"]:
        city = _clean_city_label(data.get(key))
        if city:
            return city

    for key in city_keys:
        city = _clean_city_label(data.get(key))
        if city:
            return city

    # 常见嵌套字段兜底
    for key in ["userInfo", "profile", "baseInfo", "locationInfo", "ext", "extension"]:
        value = data.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                value = None
        city = _extract_city_from_any(value)
        if city:
            return city
    return ""


def _parse_conversation(conv: dict, myid: str) -> dict | None:
    """
    解析单个会话数据

    数据结构参照 goofish-client 类型定义：
    userConvs[i] = {
        type: number,
        singleChatUserConversation: {
            singleChatConversation: { cid, pairFirst, pairSecond, extension },
            lastMessage: { message: { content, extension, ... } },
            modifyTime,
            redPoint,
        }
    }

    Args:
        conv: 原始会话数据（已解包 singleChatUserConversation）
        myid: 当前账号的用户ID

    Returns:
        格式化后的会话字典
    """
    try:
        # 获取单聊会话信息
        single_conv = conv.get("singleChatConversation", {})
        cid = single_conv.get("cid", "")
        if not cid:
            return None
        # 去掉 @goofish 后缀
        raw_cid = cid
        if "@goofish" in cid:
            cid = cid.split("@")[0]
        if not cid:
            return None

        # 通过 pairFirst/pairSecond 确定对方用户ID
        # 注意：pairFirst/pairSecond 可能带 @goofish 后缀，需先去掉再比较
        pair_first_raw = single_conv.get("pairFirst", "")
        pair_second_raw = single_conv.get("pairSecond", "")
        pair_first = pair_first_raw.split("@")[0] if "@" in pair_first_raw else pair_first_raw
        pair_second = pair_second_raw.split("@")[0] if "@" in pair_second_raw else pair_second_raw
        other_user_id = pair_second if pair_first == myid else pair_first

        # 过滤无效会话：otherUserId 为空或 "0" 的是系统/通知类会话，不返回给前端
        if not other_user_id or other_user_id == "0":
            return None

        # 从 extension 获取商品信息（可用作显示补充）
        ext = single_conv.get("extension", {})
        if isinstance(ext, str):
            try:
                ext = json.loads(ext)
            except (json.JSONDecodeError, TypeError):
                ext = {}
        item_title = ext.get("itemTitle", "") if isinstance(ext, dict) else ""
        item_id = _extract_item_id_from_any(ext)

        # 最后一条消息
        last_msg_obj = conv.get("lastMessage", {})
        last_message = last_msg_obj.get("message", {}) if last_msg_obj else {}
        last_msg_summary = _extract_message_summary(last_message)
        last_msg_time = conv.get("modifyTime", 0)

        # 未读数（redPoint）
        unread_count = conv.get("redPoint", 0)

        # 从最后一条消息的 extension 中提取对方名称
        # reminderTitle 是最后一条消息的发送者名称，需要判断是否为对方
        last_ext = last_message.get("extension", {})
        if isinstance(last_ext, str):
            try:
                last_ext = json.loads(last_ext)
            except (json.JSONDecodeError, TypeError):
                last_ext = {}
        if not item_id:
            item_id = _extract_item_id_from_any(last_ext)
        if not item_title and isinstance(last_ext, dict):
            item_title = str(
                last_ext.get("itemTitle")
                or last_ext.get("title")
                or last_ext.get("item_title")
                or ""
            )[:200]

        # 有些咨询商品信息藏在最后一条消息的 custom.data 中，额外解码一次。
        try:
            content = last_message.get("content", {}) if isinstance(last_message, dict) else {}
            custom = content.get("custom", {}) if isinstance(content, dict) else {}
            custom_data = custom.get("data", "") if isinstance(custom, dict) else ""
            if custom_data:
                decoded = json.loads(base64.b64decode(custom_data).decode("utf-8"))
                if not item_id:
                    item_id = _extract_item_id_from_any(decoded)
                if not item_title and isinstance(decoded, dict):
                    item_title = str(decoded.get("itemTitle") or decoded.get("title") or decoded.get("item_title") or "")[:200]
        except Exception:
            pass

        # v1.2.5：闲鱼有些版本会把“咨询商品”藏在会话对象更深层级，
        # 不一定在 extension.itemTitle/itemId 里。这里对完整会话做一次递归兜底提取。
        if not item_id:
            item_id = _extract_item_id_from_any(single_conv) or _extract_item_id_from_any(conv)
        if not item_title:
            item_title = (_extract_item_title_from_any(single_conv) or _extract_item_title_from_any(conv))[:200]

        sender_user_id_raw = last_ext.get("senderUserId", "") if isinstance(last_ext, dict) else ""
        sender_user_id = str(sender_user_id_raw).split("@")[0] if "@" in str(sender_user_id_raw) else str(sender_user_id_raw)
        reminder_title = last_ext.get("reminderTitle", "") if isinstance(last_ext, dict) else ""
        # 只有当最后一条消息的发送者是对方时，reminderTitle 才是对方名称
        # 用 other_user_id 比较比 myid 更可靠（避免格式不一致）
        # 昵称必须是有效的（非空、非纯数字），否则视为未获取到
        if sender_user_id and sender_user_id == other_user_id and _is_valid_nick(reminder_title):
            other_user_name = reminder_title
        else:
            other_user_name = ""

        return {
            "cid": cid,
            "rawCid": raw_cid,
            "otherUserId": other_user_id,
            "otherUserName": other_user_name,
            "otherUserAvatar": "",
            "buyerCity": _extract_city_from_any(ext),
            "itemId": item_id,
            "itemTitle": item_title,
            "lastMessageSummary": last_msg_summary,
            "lastMessageTime": last_msg_time,
            "unreadCount": unread_count,
        }
    except Exception as e:
        logger.warning(f"解析会话数据失败: {e}")
        return None


def _parse_message(model: dict, myid: str) -> dict | None:
    """
    解析单条消息

    参照 XianYuApis-master/goofish_live.py 的解析逻辑：
    - send_user_name = user_message["message"]["extension"]["reminderTitle"]
    - send_user_id   = user_message["message"]["extension"]["senderUserId"]
    - base64解码 user_message["message"]["content"]["custom"]["data"]
    - 解码后格式：
      文本: {"contentType": 1, "text": {"text": "实际消息"}}
      图片: {"contentType": 2, "image": {"pics": [{"url":"...", "width":0, "height":0}]}}

    Args:
        model: userMessageModels 中的单条数据
        myid: 当前账号的用户ID

    Returns:
        格式化后的消息字典
    """
    try:
        message = model.get("message", {})
        extension = message.get("extension", {})
        if isinstance(extension, str):
            try:
                extension = json.loads(extension)
            except (json.JSONDecodeError, TypeError):
                extension = {}

        sender_id_raw = str(extension.get("senderUserId", "") or "") if isinstance(extension, dict) else ""
        # 去掉 @goofish 后缀，保证与 myid 比较和前端显示一致
        sender_id = sender_id_raw.split("@")[0] if "@" in sender_id_raw else sender_id_raw
        sender_name = str(extension.get("reminderTitle", "") or "") if isinstance(extension, dict) else ""
        is_self = sender_id == myid

        # 解析消息内容
        content = message.get("content", {})
        custom = content.get("custom", {})
        custom_data = custom.get("data", "")

        msg_type = "text"
        msg_text = ""
        msg_images = []

        if custom_data:
            try:
                decoded = json.loads(
                    base64.b64decode(custom_data).decode("utf-8")
                )
                content_type = decoded.get("contentType", 0)

                if content_type == 1 and "text" in decoded:
                    # 文本消息: {"contentType":1, "text":{"text":"实际消息"}}
                    msg_type = "text"
                    text_obj = decoded["text"]
                    if isinstance(text_obj, dict):
                        msg_text = text_obj.get("text", "")
                    else:
                        msg_text = str(text_obj)

                elif content_type == 2 and "image" in decoded:
                    # 图片消息: {"contentType":2, "image":{"pics":[{"url":"..."}]}}
                    msg_type = "image"
                    pics = decoded.get("image", {}).get("pics", [])
                    msg_images = [
                        pic.get("url", "") for pic in pics if pic.get("url")
                    ]
                    if not msg_images:
                        msg_text = "[图片]"

                elif content_type == 3 and "audio" in decoded:
                    # 语音消息
                    msg_type = "text"
                    msg_text = "[语音消息]"

                elif "text" in decoded:
                    # 兼容: text 可能是字符串或对象
                    msg_type = "text"
                    text_obj = decoded["text"]
                    if isinstance(text_obj, dict):
                        msg_text = text_obj.get("text", str(text_obj))
                    else:
                        msg_text = str(text_obj)

                elif "picUrl" in decoded:
                    # 兼容旧格式图片
                    msg_type = "image"
                    msg_images = [decoded["picUrl"]]

                elif "title" in decoded or "template" in decoded:
                    # 卡片/系统消息
                    msg_type = "card"
                    msg_text = str(
                        decoded.get("title", decoded.get("template", "[卡片消息]"))
                    )

                else:
                    # 未知类型，用 summary 降级
                    msg_type = "text"
                    summary = custom.get("summary", "")
                    msg_text = str(summary) if summary else f"[未知消息类型:{content_type}]"

            except Exception:
                # base64解码或JSON解析失败，用 summary 降级
                summary = custom.get("summary", "")
                msg_text = str(summary) if summary else "[无法解析的消息]"
        else:
            # 没有 custom.data，用 summary 降级
            summary = custom.get("summary", "")
            msg_text = str(summary) if summary else "[系统消息]"

        # 消息时间戳（goofish IM 中字段名为 createAt）
        msg_time = message.get("createAt", 0) or message.get("time", 0)

        return {
            "messageId": str(message.get("messageId", "") or ""),
            "senderId": sender_id,
            "senderName": sender_name,
            "isSelf": is_self,
            "type": msg_type,
            "text": msg_text,
            "images": msg_images,
            "time": msg_time,
        }
    except Exception as e:
        logger.warning(f"解析消息失败: {e}")
        return None


def _extract_message_summary(message: dict) -> str:
    """
    从 lastMessage.message 提取摘要文本

    数据结构：message.content.custom.summary / message.content.custom.data (base64)
    """
    try:
        content = message.get("content", {})
        custom = content.get("custom", {})

        # 优先使用 summary 字段
        summary = custom.get("summary", "")
        if summary:
            return summary[:50]

        # 尝试解码 data (base64)
        custom_data = custom.get("data", "")
        if custom_data:
            try:
                decoded = json.loads(
                    base64.b64decode(custom_data).decode("utf-8")
                )
                if "text" in decoded:
                    text_obj = decoded["text"]
                    # text 可能是 dict（如 {"text": "实际内容"}）或字符串
                    if isinstance(text_obj, dict):
                        text = text_obj.get("text", "")
                    else:
                        text = str(text_obj)
                    return text[:50] if text else ""
                if "picUrl" in decoded:
                    return "[图片]"
                if "title" in decoded:
                    return decoded["title"][:50]
            except Exception:
                pass

        # 降级使用 degrade 字段
        degrade = custom.get("degrade", "")
        if degrade:
            return degrade[:50]
    except Exception:
        pass
    return ""
