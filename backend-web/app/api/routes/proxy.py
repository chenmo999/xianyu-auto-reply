"""
代理配置路由
管理账号的代理设置，并提供代理连通性测试。
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from loguru import logger

from app.api import deps
from common.models.user import User
from common.models.xy_account import XYAccount

router = APIRouter(tags=["代理配置"])

PROXY_TEST_HOST = "api.m.taobao.com"
PROXY_TEST_PORT = 443
PROXY_TEST_TIMEOUT = 8


# ==================== 请求/响应模型 ====================

class ProxyConfig(BaseModel):
    """代理配置"""
    proxy_type: str = "none"  # none, http, https, socks5
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_user: Optional[str] = None
    proxy_pass: Optional[str] = None


class ProxyStatus(BaseModel):
    proxy_configured: bool = False
    proxy_status: str = "unset"  # unset / success / failed
    proxy_message: str = "未设置代理"
    proxy_checked_at: Optional[str] = None


class ProxyConfigResponse(BaseModel):
    """代理配置响应"""
    success: bool
    message: str = ""
    data: Optional[ProxyConfig | dict] = None


class ProxyTestResponse(BaseModel):
    """代理测试响应"""
    success: bool
    message: str = ""
    data: Optional[ProxyStatus] = None


# ==================== 工具函数 ====================

def _proxy_configured(account: XYAccount) -> bool:
    proxy_type = (account.proxy_type or "none").lower()
    return bool(proxy_type != "none" and account.proxy_host and account.proxy_port)


def _get_proxy_status(account: XYAccount) -> ProxyStatus:
    configured = _proxy_configured(account)
    metadata = account.metadata_json if isinstance(account.metadata_json, dict) else {}
    raw = metadata.get("proxy_status") if isinstance(metadata.get("proxy_status"), dict) else {}
    if not configured:
        return ProxyStatus(
            proxy_configured=False,
            proxy_status="unset",
            proxy_message="未设置代理",
            proxy_checked_at=raw.get("checked_at"),
        )
    status = str(raw.get("status") or "failed")
    if status not in {"success", "failed", "unset"}:
        status = "failed"
    return ProxyStatus(
        proxy_configured=True,
        proxy_status=status,
        proxy_message=str(raw.get("message") or ("代理测试成功" if status == "success" else "代理未测试或测试失败")),
        proxy_checked_at=raw.get("checked_at"),
    )


def _set_proxy_status(account: XYAccount, *, configured: bool, success: bool, message: str) -> ProxyStatus:
    now = datetime.now(timezone.utc).isoformat()
    status = "success" if success else ("failed" if configured else "unset")
    metadata = dict(account.metadata_json or {})
    metadata["proxy_status"] = {
        "configured": configured,
        "status": status,
        "success": bool(success),
        "message": message,
        "checked_at": now,
    }
    account.metadata_json = metadata
    try:
        flag_modified(account, "metadata_json")
    except Exception:
        pass
    return ProxyStatus(
        proxy_configured=configured,
        proxy_status=status,
        proxy_message=message,
        proxy_checked_at=now,
    )


async def _open_proxy_connection(host: str, port: int):
    return await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=PROXY_TEST_TIMEOUT)


async def _test_http_proxy(config: ProxyConfig) -> tuple[bool, str]:
    reader = writer = None
    try:
        reader, writer = await _open_proxy_connection(config.proxy_host or "", int(config.proxy_port or 0))
        lines = [
            f"CONNECT {PROXY_TEST_HOST}:{PROXY_TEST_PORT} HTTP/1.1",
            f"Host: {PROXY_TEST_HOST}:{PROXY_TEST_PORT}",
            "User-Agent: xianyu-proxy-test/1.0",
        ]
        if config.proxy_user:
            auth_raw = f"{config.proxy_user}:{config.proxy_pass or ''}".encode("utf-8")
            auth_b64 = base64.b64encode(auth_raw).decode("ascii")
            lines.append(f"Proxy-Authorization: Basic {auth_b64}")
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), timeout=PROXY_TEST_TIMEOUT)
        data = await asyncio.wait_for(reader.read(512), timeout=PROXY_TEST_TIMEOUT)
        first_line = data.decode("latin1", errors="ignore").splitlines()[0] if data else ""
        if " 200 " in first_line or first_line.endswith(" 200"):
            return True, f"代理测试成功：HTTP CONNECT 到 {PROXY_TEST_HOST}:{PROXY_TEST_PORT} 成功"
        if "407" in first_line:
            return False, "代理认证失败：请检查代理账号/密码"
        if first_line:
            return False, f"代理连接失败：{first_line}"
        return False, "代理连接失败：代理服务器无响应"
    except asyncio.TimeoutError:
        return False, "代理测试超时：请检查代理地址、端口或网络"
    except Exception as exc:
        return False, f"代理测试失败：{exc}"
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _test_socks5_proxy(config: ProxyConfig) -> tuple[bool, str]:
    reader = writer = None
    try:
        reader, writer = await _open_proxy_connection(config.proxy_host or "", int(config.proxy_port or 0))
        methods = [0x00]
        if config.proxy_user:
            methods.append(0x02)
        writer.write(bytes([0x05, len(methods), *methods]))
        await asyncio.wait_for(writer.drain(), timeout=PROXY_TEST_TIMEOUT)
        resp = await asyncio.wait_for(reader.readexactly(2), timeout=PROXY_TEST_TIMEOUT)
        if resp[0] != 0x05:
            return False, "SOCKS5代理响应异常"
        method = resp[1]
        if method == 0xFF:
            return False, "SOCKS5代理认证方式不支持"
        if method == 0x02:
            username = (config.proxy_user or "").encode("utf-8")[:255]
            password = (config.proxy_pass or "").encode("utf-8")[:255]
            writer.write(bytes([0x01, len(username)]) + username + bytes([len(password)]) + password)
            await asyncio.wait_for(writer.drain(), timeout=PROXY_TEST_TIMEOUT)
            auth_resp = await asyncio.wait_for(reader.readexactly(2), timeout=PROXY_TEST_TIMEOUT)
            if len(auth_resp) < 2 or auth_resp[1] != 0x00:
                return False, "SOCKS5代理认证失败：请检查代理账号/密码"
        elif method != 0x00:
            return False, "SOCKS5代理返回了不支持的认证方式"

        host_bytes = PROXY_TEST_HOST.encode("idna")
        port_bytes = int(PROXY_TEST_PORT).to_bytes(2, "big")
        writer.write(bytes([0x05, 0x01, 0x00, 0x03, len(host_bytes)]) + host_bytes + port_bytes)
        await asyncio.wait_for(writer.drain(), timeout=PROXY_TEST_TIMEOUT)
        header = await asyncio.wait_for(reader.readexactly(4), timeout=PROXY_TEST_TIMEOUT)
        if header[0] != 0x05:
            return False, "SOCKS5代理连接响应异常"
        rep = header[1]
        atyp = header[3]
        # 读完剩余地址字段，避免连接未正常关闭导致异常日志。
        if atyp == 0x01:
            await reader.readexactly(4 + 2)
        elif atyp == 0x03:
            domain_len = (await reader.readexactly(1))[0]
            await reader.readexactly(domain_len + 2)
        elif atyp == 0x04:
            await reader.readexactly(16 + 2)
        if rep == 0x00:
            return True, f"代理测试成功：SOCKS5 CONNECT 到 {PROXY_TEST_HOST}:{PROXY_TEST_PORT} 成功"
        rep_messages = {
            0x01: "一般性失败",
            0x02: "连接被规则禁止",
            0x03: "网络不可达",
            0x04: "主机不可达",
            0x05: "连接被拒绝",
            0x06: "TTL超时",
            0x07: "命令不支持",
            0x08: "地址类型不支持",
        }
        return False, f"SOCKS5代理连接失败：{rep_messages.get(rep, f'错误码 {rep}')}"
    except asyncio.TimeoutError:
        return False, "代理测试超时：请检查代理地址、端口或网络"
    except Exception as exc:
        return False, f"SOCKS5代理测试失败：{exc}"
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _test_proxy_config(config: ProxyConfig) -> tuple[bool, str, bool]:
    proxy_type = (config.proxy_type or "none").lower()
    configured = bool(proxy_type != "none" and config.proxy_host and config.proxy_port)
    if not configured:
        return False, "未设置代理", False
    if proxy_type not in {"http", "https", "socks5"}:
        return False, "代理类型不支持", True
    if proxy_type in {"http", "https"}:
        ok, message = await _test_http_proxy(config)
    else:
        ok, message = await _test_socks5_proxy(config)
    return ok, message, True


async def _get_account_for_current_user(account_id: str, current_user: User, session: AsyncSession) -> Optional[XYAccount]:
    stmt = select(XYAccount).where(
        XYAccount.owner_id == current_user.id,
        XYAccount.account_id == account_id,
    )
    result = await session.execute(stmt)
    return result.scalars().first()


# ==================== 路由 ====================

@router.get("/{account_id}", response_model=ProxyConfigResponse)
async def get_proxy_config(
    account_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    session: AsyncSession = Depends(deps.get_db_session),
):
    """获取账号的代理配置"""
    try:
        account = await _get_account_for_current_user(account_id, current_user, session)
        if not account:
            return ProxyConfigResponse(success=False, message="账号不存在或无权限访问")
        proxy_status = _get_proxy_status(account)
        return ProxyConfigResponse(
            success=True,
            data={
                "proxy_type": account.proxy_type or "none",
                "proxy_host": account.proxy_host,
                "proxy_port": account.proxy_port,
                "proxy_user": account.proxy_user,
                "proxy_pass": account.proxy_pass,
                **proxy_status.model_dump(),
            },
        )
    except Exception as e:
        logger.error(f"获取代理配置失败: {e}")
        return ProxyConfigResponse(success=False, message=f"获取代理配置失败: {str(e)}")


@router.put("/{account_id}", response_model=ProxyConfigResponse)
async def update_proxy_config(
    account_id: str,
    config: ProxyConfig,
    current_user: User = Depends(deps.get_current_active_user),
    session: AsyncSession = Depends(deps.get_db_session),
):
    """更新账号的代理配置。保存后自动测试代理，测试结果写入账号元数据。"""
    try:
        config.proxy_type = (config.proxy_type or "none").lower()
        valid_proxy_types = ["none", "http", "https", "socks5"]
        if config.proxy_type not in valid_proxy_types:
            return ProxyConfigResponse(success=False, message=f"无效的代理类型，支持的类型: {', '.join(valid_proxy_types)}")
        if config.proxy_type != "none":
            if not config.proxy_host:
                return ProxyConfigResponse(success=False, message="代理地址不能为空")
            if not config.proxy_port or config.proxy_port <= 0:
                return ProxyConfigResponse(success=False, message="代理端口无效")

        account = await _get_account_for_current_user(account_id, current_user, session)
        if not account:
            return ProxyConfigResponse(success=False, message="账号不存在或无权限访问")

        account.proxy_type = config.proxy_type
        account.proxy_host = config.proxy_host.strip() if config.proxy_type != "none" and config.proxy_host else None
        account.proxy_port = config.proxy_port if config.proxy_type != "none" else None
        account.proxy_user = config.proxy_user.strip() if config.proxy_type != "none" and config.proxy_user else None
        account.proxy_pass = config.proxy_pass if config.proxy_type != "none" and config.proxy_pass else None

        saved_config = ProxyConfig(
            proxy_type=account.proxy_type or "none",
            proxy_host=account.proxy_host,
            proxy_port=account.proxy_port,
            proxy_user=account.proxy_user,
            proxy_pass=account.proxy_pass,
        )
        ok, test_message, configured = await _test_proxy_config(saved_config)
        proxy_status = _set_proxy_status(account, configured=configured, success=ok, message=test_message)

        session.add(account)
        await session.commit()

        logger.info(f"更新账号 {account_id} 代理配置: {config.proxy_type}, test={proxy_status.proxy_status}, msg={test_message}")
        if not configured:
            message = "代理配置已清除"
        elif ok:
            message = "代理配置已保存，代理测试成功"
        else:
            message = f"代理配置已保存，但测试失败：{test_message}"
        return ProxyConfigResponse(
            success=True,
            message=message,
            data={
                "proxy_type": account.proxy_type or "none",
                "proxy_host": account.proxy_host,
                "proxy_port": account.proxy_port,
                "proxy_user": account.proxy_user,
                "proxy_pass": account.proxy_pass,
                **proxy_status.model_dump(),
            },
        )
    except Exception as e:
        logger.error(f"更新代理配置失败: {e}")
        await session.rollback()
        return ProxyConfigResponse(success=False, message=f"更新代理配置失败: {str(e)}")


@router.post("/{account_id}/test", response_model=ProxyTestResponse)
async def test_proxy_config(
    account_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    session: AsyncSession = Depends(deps.get_db_session),
):
    """测试已保存的账号代理配置，并更新账号代理状态。"""
    try:
        account = await _get_account_for_current_user(account_id, current_user, session)
        if not account:
            return ProxyTestResponse(success=False, message="账号不存在或无权限访问")
        config = ProxyConfig(
            proxy_type=account.proxy_type or "none",
            proxy_host=account.proxy_host,
            proxy_port=account.proxy_port,
            proxy_user=account.proxy_user,
            proxy_pass=account.proxy_pass,
        )
        ok, test_message, configured = await _test_proxy_config(config)
        proxy_status = _set_proxy_status(account, configured=configured, success=ok, message=test_message)
        session.add(account)
        await session.commit()
        return ProxyTestResponse(success=ok, message=test_message, data=proxy_status)
    except Exception as e:
        logger.error(f"测试代理配置失败: {e}")
        await session.rollback()
        return ProxyTestResponse(success=False, message=f"测试代理配置失败: {str(e)}")


@router.delete("/{account_id}", response_model=ProxyConfigResponse)
async def clear_proxy_config(
    account_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    session: AsyncSession = Depends(deps.get_db_session),
):
    """清除账号的代理配置"""
    try:
        account = await _get_account_for_current_user(account_id, current_user, session)
        if not account:
            return ProxyConfigResponse(success=False, message="账号不存在或无权限访问")

        account.proxy_type = "none"
        account.proxy_host = None
        account.proxy_port = None
        account.proxy_user = None
        account.proxy_pass = None
        proxy_status = _set_proxy_status(account, configured=False, success=False, message="未设置代理")

        session.add(account)
        await session.commit()

        logger.info(f"清除账号 {account_id} 代理配置")
        return ProxyConfigResponse(
            success=True,
            message="代理配置已清除",
            data={"proxy_type": "none", **proxy_status.model_dump()},
        )
    except Exception as e:
        logger.error(f"清除代理配置失败: {e}")
        await session.rollback()
        return ProxyConfigResponse(success=False, message=f"清除代理配置失败: {str(e)}")
