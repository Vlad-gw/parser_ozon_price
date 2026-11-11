"""Общие типы и безопасный разбор ссылок товара."""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx


class ScrapeError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ProductRef:
    marketplace: str
    sku: str
    url: str


@dataclass(frozen=True)
class Quote:
    name: str
    regular_cents: int | None
    card_cents: int | None = None
    available: bool | None = None
    source: str = ""
    image_url: str | None = None


def money_to_cents(value: str | float | int | Decimal) -> int:
    try:
        amount = Decimal(str(value).replace(',', '.'))
        cents = int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise ValueError('Некорректная сумма') from None
    if cents <= 0 or cents > 100_000_000_00:
        raise ValueError('Цена должна быть положительной и разумной')
    return cents


def rub_text_to_cents(value: object) -> int | None:
    """'12 340 ₽' / '12 340,50 ₽' / 12340 -> копейки; 0 или мусор -> None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return money_to_cents(value)
        except ValueError:
            return None
    if not isinstance(value, str):
        return None
    s = value.replace('\u00a0', ' ').replace('\u202f', ' ').strip()
    match = re.search(r'\d{1,3}(?:\s\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?', s)
    if not match:
        return None
    try:
        return money_to_cents(match.group(0).replace(' ', ''))
    except ValueError:
        return None


def parse_ref(value: str, platform: str | None = None) -> ProductRef:
    """Accept only Ozon product URLs or a numeric Ozon product ID.

    Never request a user-supplied arbitrary host (SSRF guard).
    A short /t/ URL is handled separately by resolve_ref().
    """
    v = value.strip()
    if platform not in (None, 'ozon'):
        raise ValueError('Поддерживается только Ozon')
    if re.fullmatch(r'\d{5,15}', v):
        return ProductRef('ozon', v, f'https://www.ozon.ru/product/{v}/')
    try:
        u = urlsplit(v)
        has_port = u.port is not None
    except ValueError:
        raise ValueError('Некорректная ссылка') from None
    if (u.scheme != 'https' or u.username or u.password or has_port or
            u.hostname not in ('ozon.ru', 'www.ozon.ru')):
        raise ValueError('Нужна HTTPS-ссылка на товар Ozon (www.ozon.ru)')
    m = re.fullmatch(r'/products?/([^/]+)/?', u.path, re.I)
    slug = m.group(1) if m else ''
    sku_match = re.search(r'(?:^|-)(\d{5,15})$', slug)
    if not sku_match:
        raise ValueError('Нужна ссылка Ozon вида /product/НАЗВАНИЕ-АРТИКУЛ/ либо артикул')
    sku = sku_match.group(1)
    return ProductRef('ozon', sku, f'https://www.ozon.ru{u.path.rstrip("/")}/')


def _ozon_short_code(value: str) -> str | None:
    """Return only an exact, public Ozon share code; never fetch arbitrary URLs."""
    try:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or parts.hostname not in ('ozon.ru', 'www.ozon.ru')
                or parts.username or parts.password or parts.port is not None):
            return None
    except ValueError:
        return None
    match = re.fullmatch(r'/t/([A-Za-z0-9_-]{4,64})/?', parts.path)
    return match.group(1) if match else None


def _safe_ozon_location(current: str, location: str) -> str:
    """Validate EVERY redirect before following it (SSRF / wrong-product guard)."""
    target = urljoin(current, location)
    try:
        url = urlsplit(target)
        safe = (url.scheme == 'https'
                and url.hostname in ('ozon.ru', 'www.ozon.ru')
                and url.username is None and url.password is None
                and url.port is None)
    except ValueError:
        safe = False
    if not safe:
        raise ValueError('Короткая ссылка Ozon перенаправляет на посторонний адрес')
    return target


def _validated_product_from_url(value: str) -> ProductRef | None:
    """Never infer SKU from an arbitrary response body or unrelated URL."""
    try:
        return parse_ref(value, 'ozon')
    except ValueError:
        return None


async def _resolve_short_in_chrome(code: str) -> ProductRef:
    """Fallback for Ozon 403: follow the share link in the user's local CDP Chrome.

    Only the /t/ code is opened, not any arbitrary user-controlled host.
    Navigations to non-Ozon document domains are denied. No CAPTCHA solving.
    """
    cdp = os.getenv('PRICE_TRACKER_CDP_URL', '').strip()
    try:
        cdp_u = urlsplit(cdp)
        allowed_cdp = (cdp_u.scheme == 'http'
                       and cdp_u.hostname in ('localhost', '127.0.0.1', '::1')
                       and cdp_u.port is not None
                       and not cdp_u.username and not cdp_u.password
                       and cdp_u.path in ('', '/') and not cdp_u.query and not cdp_u.fragment)
    except ValueError:
        allowed_cdp = False
    if not allowed_cdp:
        raise ValueError('Не удалось раскрыть короткую ссылку: нет подключения к локальному Chrome. '
                         'Запустите приложение через bash start.command.')

    from playwright.async_api import Error as PlaywrightError, async_playwright
    browser = None
    try:
        async with async_playwright() as playwright:
            # On macOS CDP can be bound to either IPv4 or IPv6 loopback.
            last_error = None
            endpoints = list(dict.fromkeys([cdp, f'http://127.0.0.1:{cdp_u.port}',
                                            f'http://[::1]:{cdp_u.port}']))
            for endpoint in endpoints:
                try:
                    browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=3500)
                    break
                except PlaywrightError as exc:
                    last_error = exc
            if browser is None:
                raise ValueError('Chrome для Price Tracker недоступен. '
                                 'Запустите bash start.command и повторите попытку.') from last_error
            try:
                if not browser.contexts:
                    raise ValueError('Chrome не вернул профиль браузера')
                context = browser.contexts[0]

                # Share links often leave ?short=CODE on the destination tab.
                # A known product URL with this code can be reused without new requests.
                for opened in context.pages:
                    parts = urlsplit(opened.url)
                    if (parts.hostname in ('ozon.ru', 'www.ozon.ru')
                            and code in parse_qs(parts.query).get('short', [])):
                        product = _validated_product_from_url(opened.url)
                        if product is not None:
                            return product

                from .cdp_worker import ozon_background_page
                async with ozon_background_page(browser, context) as tab:
                    async def restrict_document_redirects(route):
                        request = route.request
                        if request.resource_type == 'document':
                            try:
                                parts = urlsplit(request.url)
                                valid = (parts.scheme == 'https'
                                         and parts.hostname in ('ozon.ru', 'www.ozon.ru'))
                            except ValueError:
                                valid = False
                            if not valid:
                                await route.abort()
                                return
                        await route.continue_()

                    await tab.route('**/*', restrict_document_redirects)
                    try:
                        # Background worker stays minimized and is reused for
                        # price fetching. Never focus or close a user-visible tab.
                        try:
                            await tab.goto(f'https://ozon.ru/t/{code}',
                                           wait_until='domcontentloaded', timeout=15000)
                        except PlaywrightError:
                            pass
                        for _ in range(15):
                            product = _validated_product_from_url(tab.url)
                            if product is not None:
                                return product
                            await tab.wait_for_timeout(200)
                        raise ValueError('Chrome не смог раскрыть короткую ссылку Ozon. '
                                         'Проверьте, что сайт открывается в специальном Chrome.')
                    finally:
                        await tab.unroute('**/*', restrict_document_redirects)
            finally:
                await browser.close()  # CDP disconnect; keeps the user's Chrome running.
    except PlaywrightError as exc:
        raise ValueError(f'Ошибка подключения к Chrome при раскрытии ссылки: {type(exc).__name__}') from exc


async def resolve_ref(value: str, platform: str | None = None, *,
                      client: httpx.AsyncClient | None = None) -> ProductRef:
    """Convert /t/ Ozon share links to a stable /product/SKU/ URL.

    Fast path is a guarded HTTP redirect chain. When the storefront blocks
    httpx, the existing local Chrome CDP session is used automatically.
    """
    v = value.strip()
    code = _ozon_short_code(v)
    if code is None:
        return parse_ref(v, platform)
    if platform not in (None, 'ozon'):
        raise ValueError('Поддерживается только Ozon')

    # Strip tracking query before making the request.
    current = f'https://ozon.ru/t/{code}'
    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=8, follow_redirects=False)
    try:
        for _ in range(5):
            try:
                response = await client.get(current, follow_redirects=False)
            except httpx.RequestError:
                break  # Browser fallback can handle network restrictions.
            if response.status_code not in (301, 302, 303, 307, 308):
                break  # 403/200 without Location -> use Chrome instead.
            location = response.headers.get('location')
            if not location:
                break
            target = _safe_ozon_location(current, location)
            product = _validated_product_from_url(target)
            if product:
                return product
            next_code = _ozon_short_code(target)
            if next_code is None:
                raise ValueError('Короткая ссылка Ozon ведёт не на карточку товара')
            current = f'https://{urlsplit(target).hostname}/t/{next_code}'
    finally:
        if owned:
            await client.aclose()

    return await _resolve_short_in_chrome(code)


class RateGate:
    """Минимальная пауза между запросами в рамках одного процесса."""
    def __init__(self, min_interval_seconds: float):
        self.interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            remaining = self.interval - (time.monotonic() - self._last)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last = time.monotonic()
