"""Reusable Chrome CDP worker that does not take focus from the user.

A single minimized Chrome window is reused for Ozon share-link resolution and
price checks. A background *tab* is used only if Chrome doesn't support minimized
windows. We deliberately avoid Chrome's experimental `hidden` target: Chrome
blocks ordinary storefront navigation in those targets.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import Error as PlaywrightError

LOG = logging.getLogger(__name__)
_LOCK = asyncio.Lock()
_TARGET_FILE = Path(__file__).resolve().parents[2] / 'data' / '.ozon-worker-target.json'


async def _get_page_target_id(context, page) -> str | None:
    session = None
    try:
        session = await context.new_cdp_session(page)
        info = await session.send('Target.getTargetInfo')
        return (info.get('targetInfo') or {}).get('targetId')
    except PlaywrightError:
        return None
    finally:
        if session is not None:
            try:
                await session.detach()
            except PlaywrightError:
                pass


async def _find_page(context, target_id: str):
    for page in context.pages:
        if page.is_closed():
            continue
        if await _get_page_target_id(context, page) == target_id:
            return page
    return None


def _read_saved_target() -> str | None:
    try:
        item = json.loads(_TARGET_FILE.read_text(encoding='utf-8'))
        target = item.get('target_id')
        if isinstance(target, str) and 10 <= len(target) <= 120 and target.isalnum():
            return target
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _save_target(target_id: str) -> None:
    _TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _TARGET_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps({'target_id': target_id}), encoding='utf-8')
    tmp.replace(_TARGET_FILE)


async def _minimize(cdp, target_id: str) -> None:
    try:
        info = await cdp.send('Browser.getWindowForTarget', {'targetId': target_id})
        await cdp.send('Browser.setWindowBounds', {
            'windowId': info['windowId'],
            'bounds': {'windowState': 'minimized'},
        })
    except (PlaywrightError, KeyError):
        # macOS Chrome versions differ in how native windows are exposed via CDP.
        pass


async def _acquire_worker(browser, context, cdp):
    target_id = _read_saved_target()
    if target_id:
        page = await _find_page(context, target_id)
        if page is not None:
            await _minimize(cdp, target_id)
            return page

    # Chrome supports a minimized window. Creating it at about:blank means no
    # Ozon URL or other user's tab receives focus on worker initialization.
    try:
        target = await cdp.send('Target.createTarget', {
            'url': 'about:blank',
            'newWindow': True,
            'windowState': 'minimized',
            'background': True,
            'focus': False,
        })
    except PlaywrightError as exc:
        LOG.warning('Chrome cannot create minimized window; using background tab: %s', exc)
        # This still does not steal focus; unlike context.new_page(), the tab
        # remains open and is reused rather than flashing in/out per request.
        target = await cdp.send('Target.createTarget', {
            'url': 'about:blank', 'background': True, 'focus': False,
        })

    target_id = target['targetId']
    try:
        for _ in range(40):
            page = await _find_page(context, target_id)
            if page is not None:
                _save_target(target_id)
                await _minimize(cdp, target_id)
                return page
            await asyncio.sleep(0.1)
        raise RuntimeError('Chrome создал служебную страницу, но Playwright её не обнаружил')
    except Exception:
        try:
            await cdp.send('Target.closeTarget', {'targetId': target_id})
        except PlaywrightError:
            pass
        raise


@asynccontextmanager
async def ozon_background_page(browser, context):
    """Yield the same background page across short-links and hourly checks.

    Never close this page after one operation. Lock navigation, as user-initiated
    Telegram checks and scheduled checks may run concurrently.
    """
    async with _LOCK:
        cdp = await browser.new_browser_cdp_session()
        try:
            page = await _acquire_worker(browser, context, cdp)
            yield page
        finally:
            try:
                await cdp.detach()
            except PlaywrightError:
                pass
