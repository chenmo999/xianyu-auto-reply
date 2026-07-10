"""
1688 服务器浏览器登录服务

第一阶段能力：
1. 后端启动 Playwright Chromium 打开 1688 登录页
2. 前端后续可以轮询截图
3. 前端后续可以发送点击、输入、按键操作
4. 登录完成后后端自动读取 Cookie / storage_state
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright


ALI1688_LOGIN_URL = "https://login.1688.com/member/signin.htm"


@dataclass
class Ali1688LoginSession:
    owner_id: int
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    created_at: float
    updated_at: float


class Ali1688LoginService:
    """1688 登录会话管理器。会话先存在内存里，登录完成后再写数据库。"""

    def __init__(self) -> None:
        self._sessions: dict[int, Ali1688LoginSession] = {}
        self._lock = asyncio.Lock()

    async def start(self, owner_id: int) -> dict[str, Any]:
        """启动或重启某个用户的 1688 登录浏览器"""
        async with self._lock:
            await self.close(owner_id)

            p = await async_playwright().start()
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
            )
            page = await context.new_page()
            await page.goto(ALI1688_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

            now = time.time()
            self._sessions[owner_id] = Ali1688LoginSession(
                owner_id=owner_id,
                playwright=p,
                browser=browser,
                context=context,
                page=page,
                created_at=now,
                updated_at=now,
            )

            return {
                "started": True,
                "url": page.url,
                "title": await page.title(),
            }

    def _get_session(self, owner_id: int) -> Ali1688LoginSession:
        session = self._sessions.get(owner_id)
        if not session:
            raise RuntimeError("1688登录会话不存在，请先打开1688服务器登录")
        session.updated_at = time.time()
        return session

    async def screenshot(self, owner_id: int) -> dict[str, Any]:
        """获取当前浏览器截图，返回 base64 图片"""
        session = self._get_session(owner_id)
        img = await session.page.screenshot(type="jpeg", quality=75, full_page=False)
        return {
            "image": "data:image/jpeg;base64," + base64.b64encode(img).decode("utf-8"),
            "url": session.page.url,
            "title": await session.page.title(),
        }

    async def click(self, owner_id: int, x: float, y: float) -> dict[str, Any]:
        """点击页面坐标"""
        session = self._get_session(owner_id)
        await session.page.mouse.click(x, y)
        return {"success": True}

    async def type_text(self, owner_id: int, text: str) -> dict[str, Any]:
        """向当前焦点输入文字"""
        session = self._get_session(owner_id)
        await session.page.keyboard.type(text, delay=30)
        return {"success": True}

    async def press(self, owner_id: int, key: str) -> dict[str, Any]:
        """按键，例如 Enter、Tab、Backspace"""
        session = self._get_session(owner_id)
        await session.page.keyboard.press(key)
        return {"success": True}

    async def finish(self, owner_id: int) -> dict[str, Any]:
        """登录完成后，读取 Cookie 和 storage_state"""
        session = self._get_session(owner_id)

        cookies = await session.context.cookies()
        storage_state = await session.context.storage_state()

        cookie_str = "; ".join(
            f"{c.get('name')}={c.get('value')}"
            for c in cookies
            if c.get("name") and c.get("value") is not None
        )

        return {
            "cookie": cookie_str,
            "storage_state": storage_state,
            "cookie_count": len(cookies),
            "url": session.page.url,
            "title": await session.page.title(),
        }

    async def close(self, owner_id: int) -> None:
        """关闭某个用户的登录浏览器"""
        session = self._sessions.pop(owner_id, None)
        if not session:
            return

        try:
            await session.context.close()
        except Exception as exc:
            logger.warning(f"关闭1688 context失败: {exc}")

        try:
            await session.browser.close()
        except Exception as exc:
            logger.warning(f"关闭1688 browser失败: {exc}")

        try:
            await session.playwright.stop()
        except Exception as exc:
            logger.warning(f"停止1688 playwright失败: {exc}")


ali1688_login_service = Ali1688LoginService()
