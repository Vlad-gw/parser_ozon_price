"""Ozon buyer storefront: page JSON, product JSON-LD, persistent local Chrome fallback.

No CAPTCHA solving, proxy rotation or synthetic prices. Browser profile is separate
from the user's normal Chrome profile, stored locally under data/browser-profiles/.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright, Error as PlaywrightError

from .common import ProductRef, Quote, RateGate, ScrapeError, rub_text_to_cents

_OZON_GATE = RateGate(3.0)
_PROFILE_LOCK = asyncio.Lock()
_PROFILE = Path(__file__).resolve().parents[2] / 'data' / 'browser-profiles' / 'ozon'
_BLOCKED_CODES = (401, 403, 429, 498)
_CHALLENGE_MARKERS = ('captcha', 'капча', 'проверка браузера', 'доступ ограничен',
                      'access denied', 'variti', 'подтвердите, что вы')


def parse_ozon_widget_payload(payload: dict, sku: str, *, verified_page: bool = False) -> Quote | None:
    """Only the main product's widget; never use an unrelated recommendation price.

    verified_page=True is allowed ONLY for a JSON response whose requested product URL
    contains the tracked SKU. For unlinked price widgets it additionally requires
    precisely one price widget and a main product heading.
    """
    states = payload.get('widgetStates')
    if not isinstance(states, dict):
        return None
    parsed = {}
    for key, raw in states.items():
        if not isinstance(key, str) or not (key.startswith('webPrice-') or key.startswith('webProductHeading-')):
            continue
        try:
            val = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        if isinstance(val, dict):
            parsed[key] = val
    name = next((v.get('title') for k, v in parsed.items()
                 if k.startswith('webProductHeading-') and isinstance(v.get('title'), str)), '')
    prices = [(key, state) for key, state in parsed.items() if key.startswith('webPrice-')]
    for _key, state in prices:
        link = str(state.get('link') or '')
        link_id = re.search(r'(?:[?&]product_id=)(\d+)', link)
        if link_id:
            if link_id.group(1) != sku:
                continue
        elif not (verified_page and name and len(prices) == 1):
            continue
        if state.get('isAvailable') is False:
            return Quote(name or f'Ozon #{sku}', None, None, available=False, source='ozon:widgetStates')
        regular = rub_text_to_cents(state.get('price'))
        card = rub_text_to_cents(state.get('cardPrice'))
        if regular is not None or card is not None:
            return Quote(name or f'Ozon #{sku}', regular, card, available=True, source='ozon:widgetStates')
    return None


def parse_json_ld(html_ld_scripts: list[str], sku: str) -> Quote | None:
    """Use only a Product with matching SKU, or a unique top-level Product."""
    for raw in html_ld_scripts:
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        nodes = obj if isinstance(obj, list) else [obj]
        expanded = []
        for node in nodes:
            if isinstance(node, dict) and isinstance(node.get('@graph'), list):
                expanded.extend(node['@graph'])
            else:
                expanded.append(node)
        products = [node for node in expanded if isinstance(node, dict) and
                    'Product' in (node.get('@type') if isinstance(node.get('@type'), list)
                                  else [node.get('@type')])]
        for node in products:
            node_sku = str(node.get('sku') or node.get('productID') or '')
            if node_sku and node_sku != sku:
                continue
            # A product without an identifier is only trusted if unambiguous.
            if not node_sku and len(products) != 1:
                continue
            offers = node.get('offers')
            if isinstance(offers, list):
                offers = offers[0] if len(offers) == 1 else None
            if not isinstance(offers, dict):
                continue
            if offers.get('priceCurrency') not in ('RUB', 'RUR', None):
                continue
            availability_text = str(offers.get('availability') or '').lower()
            if 'outofstock' in availability_text:
                return Quote(str(node.get('name') or f'Ozon #{sku}'), None,
                             available=False, source='ozon:json-ld')
            price = rub_text_to_cents(offers.get('price'))
            if price is not None:
                image = node.get('image')
                if isinstance(image, list):
                    image = image[0] if image else None
                if isinstance(image, dict):
                    image = image.get('url')
                image_url = None
                if isinstance(image, str) and len(image) <= 800:
                    image_parts = urlsplit(image)
                    if image_parts.scheme == 'https' and image_parts.hostname and (
                        image_parts.hostname == 'ozon.ru' or image_parts.hostname.endswith('.ozon.ru') or
                        image_parts.hostname == 'ozone.ru' or image_parts.hostname.endswith('.ozone.ru')
                    ):
                        image_url = image
                return Quote(str(node.get('name') or f'Ozon #{sku}'), price,
                             available=True, source='ozon:json-ld', image_url=image_url)
    return None


def _quote_from_seo(payload: dict, sku: str) -> Quote | None:
    """Ozon sometimes sends schema.org Product JSON in seo.script instead of HTML."""
    seo = payload.get('seo')
    if not isinstance(seo, dict):
        return None
    snippets = []
    for script in seo.get('script') or []:
        if isinstance(script, dict):
            content = script.get('innerHTML') or script.get('textContent')
            if isinstance(content, str):
                snippets.append(content)
    return parse_json_ld(snippets, sku)


def _is_matching_open_tab(url: str, sku: str) -> bool:
    """Accept only an actual Ozon product tab with the exact tracked SKU."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in ('ozon.ru', 'www.ozon.ru'):
        return False
    match = re.fullmatch(r'/products?/([^/]+)/?', parts.path, re.I)
    return bool(match and re.search(r'(?:^|-)' + re.escape(sku) + r'$', match.group(1)))


def parse_visible_price_widget(text: str, name: str, *, struck_prices: set[int] | None = None) -> Quote | None:
    """Only main product price labels; crossed original price is not purchasable."""
    text = text.replace('\u00a0', ' ').replace('\u202f', ' ')
    money_re = re.compile(r'(?<!\d)(\d[\d\s]{0,18}(?:[.,]\d{1,2})?)\s*₽')
    prices = []
    for m in money_re.finditer(text):
        amount = rub_text_to_cents(m.group(1))
        if amount is not None and amount not in (struck_prices or set()):
            prices.append((m.start(), m.end(), amount))
    if not prices or len(prices) > 8:
        return None

    labels = [
        ('card', re.compile(r'(?:с\s+банками(?!\s+не)|с\s+(?:ozon|озон)\s+картой|цена\s+с\s+картой)', re.I)),
        ('regular', re.compile(r'(?:с\s+другими\s+банками|без\s+(?:ozon\s+)?карты|обычн\w*\s+цен\w*)', re.I)),
    ]
    found: dict[str, int] = {}
    for kind, pattern in labels:
        for match in pattern.finditer(text):
            before = [(start, end, amount) for start, end, amount in prices
                      if 0 <= match.start() - end <= 70]
            after = [(start, end, amount) for start, end, amount in prices
                     if 0 <= start - match.end() <= 35]
            # A bank label may follow both the purchasable regular price and
            # a crossed-out seller reference price. Unless DOM positively
            # identifies the crossed-out value, do not guess between them.
            other_kind = 'card' if kind == 'regular' else 'regular'
            before = [row for row in before if row[2] != found.get(other_kind)]
            if len(before) == 1:
                candidate = before[0][2]
            elif len(before) > 1:
                continue  # ambiguous: never mistake 7 000 ₽ for 657 ₽
            elif len(after) == 1:
                candidate = after[0][2]
            else:
                continue
            if kind not in found:
                found[kind] = candidate
            elif found[kind] != candidate:
                return None

    unclassified = False
    if not found:
        if len(prices) == 1:
            found['regular'] = prices[0][2]
            unclassified = True
        else:
            return None
    if found.get('regular') is not None and found.get('card') is not None:
        if found['card'] > found['regular']:
            return None
    return Quote(name, found.get('regular'), found.get('card'),
                 available=None, source='ozon:visible-unclassified' if unclassified else 'ozon:visible-tab')


async def _visible_widget_quote(page, ref: ProductRef) -> Quote | None:
    """Inspect exactly one product price widget and exclude crossed-out values."""
    if not _is_matching_open_tab(page.url, ref.sku):
        return None
    try:
        widgets = page.locator('[data-widget="webPrice"]')
        if await widgets.count() != 1:
            return None
        title = page.locator('[data-widget="webProductHeading"] h1, h1')
        name = (await title.first.inner_text(timeout=2000)).strip() if await title.count() else f'Ozon #{ref.sku}'
        widget = widgets.first
        text = await widget.inner_text(timeout=4000)
        crossed = await widget.evaluate('''el => {
          const result = new Set();
          for (const node of el.querySelectorAll('*')) {
            if (node.children.length) continue;
            let struck = false, parent = node;
            while (parent && parent !== el) {
              const style = getComputedStyle(parent);
              if (parent.matches('s,del') || style.textDecorationLine.includes('line-through')) {
                struck = true; break;
              }
              parent = parent.parentElement;
            }
            if (struck) result.add(node.innerText || node.textContent || '');
          }
          return [...result];
        }''')
        crossed_amounts = {amount for raw in crossed
                           if (amount := rub_text_to_cents(raw)) is not None}
        return parse_visible_price_widget(text, name, struck_prices=crossed_amounts)
    except PlaywrightError:
        return None


async def _read_existing_tab(page, ref: ProductRef) -> Quote | None:
    """Read data already rendered in a user-opened CDP tab; do not navigate.

    Unlike context.new_page()/goto, this uses the exact page the user sees.
    No CAPTCHA solving, cookies export, or new requests are performed here.
    """
    if not _is_matching_open_tab(page.url, ref.sku):
        return None
    try:
        # A visible tab is not a live endpoint. Never record its hours-old DOM
        # as a fresh hourly price. Users can manually refresh it before a check.
        page_age_seconds = await page.evaluate('Math.max(0, (Date.now() - performance.timeOrigin) / 1000)')
        if not isinstance(page_age_seconds, (int, float)) or page_age_seconds > 600:
            return None
        await page.wait_for_timeout(1000)
        body = (await page.locator('body').inner_text(timeout=4000))[:1000].lower()
        if any(marker in body for marker in _CHALLENGE_MARKERS):
            return None
        visible = await _visible_widget_quote(page, ref)
        if visible:
            return visible
        scripts = await page.locator('script[type="application/ld+json"]').all_text_contents()
        return parse_json_ld(scripts, ref.sku)
    except PlaywrightError:
        return None


async def _read_page(context, ref: ProductRef, timeout_ms: int, *, headed: bool,
                     worker_page=None) -> Quote:
    page = worker_page if worker_page is not None else await context.new_page()
    pending: set[asyncio.Task] = set()
    captured: list[dict] = []

    async def read_response(response):
        if '/api/' not in response.url or 'page/json/v2' not in response.url:
            return
        # The response may include cards for ads or similar products: anchor to URL.
        if ref.sku not in unquote(response.url) or response.status != 200:
            return
        try:
            payload = await response.json()
            if isinstance(payload, dict):
                captured.append(payload)
        except (ValueError, PlaywrightError):
            pass

    def on_response(response):
        task = asyncio.create_task(read_response(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    page.on('response', on_response)
    try:
        result = await page.goto(ref.url, wait_until='domcontentloaded', timeout=timeout_ms)
        status = result.status if result else None
        if status in _BLOCKED_CODES:
            raise ScrapeError('blocked', f'Ozon ограничил доступ в Chrome: HTTP {status}')
        if status and status >= 500:
            raise ScrapeError('http', f'Ozon вернул HTTP {status}')
        if ref.sku not in unquote(page.url):
            raise ScrapeError('not_found', 'Ozon перенаправил на другую карточку; цену не записываем')
        await page.wait_for_timeout(1800)
        title = (await page.title()).lower()
        body = (await page.locator('body').inner_text(timeout=5000))[:1500].lower()
        if any(marker in title or marker in body for marker in _CHALLENGE_MARKERS):
            if headed:
                # If the site offers an ordinary human verification, the user can do it.
                await page.wait_for_timeout(12000)
                title = (await page.title()).lower()
                body = (await page.locator('body').inner_text(timeout=5000))[:1500].lower()
            if any(marker in title or marker in body for marker in _CHALLENGE_MARKERS):
                raise ScrapeError('blocked', 'Ozon показывает проверку в Chrome; доступ не предоставлен')
        if pending:
            await asyncio.wait(pending, timeout=3)
        for payload in captured:
            quote = parse_ozon_widget_payload(payload, ref.sku, verified_page=True)
            if quote:
                if quote.regular_cents is None or quote.card_cents is None:
                    dom = await _visible_widget_quote(page, ref)
                    if dom:
                        return Quote(quote.name, dom.regular_cents or quote.regular_cents,
                                     dom.card_cents or quote.card_cents,
                                     available=quote.available, source=quote.source + '+visible')
                return quote
            quote = _quote_from_seo(payload, ref.sku)
            if quote:
                dom = await _visible_widget_quote(page, ref)
                if dom and dom.regular_cents is not None and dom.card_cents is not None:
                    return Quote(quote.name, dom.regular_cents, dom.card_cents,
                                 available=quote.available, source=dom.source)
                return quote

        # Same-origin request, not a separate synthetic HTTP client. Treat 403 as a hard stop.
        path = urlsplit(ref.url).path
        result = await page.evaluate('''async (path) => {
          try {
            const url = '/api/entrypoint-api.bx/page/json/v2?url=' + encodeURIComponent(path);
            const response = await fetch(url, {credentials:'same-origin', headers:{accept:'application/json'}});
            if (!response.ok) return {status: response.status};
            return {status: response.status, payload: await response.json()};
          } catch (err) {return {error: String(err)}}
        }''', path)
        api_blocked = False
        if isinstance(result, dict):
            data = result.get('payload')
            if isinstance(data, dict):
                quote = parse_ozon_widget_payload(data, ref.sku, verified_page=True)
                if quote:
                    if quote.regular_cents is None or quote.card_cents is None:
                        dom = await _visible_widget_quote(page, ref)
                        if dom:
                            return Quote(quote.name, dom.regular_cents or quote.regular_cents,
                                         dom.card_cents or quote.card_cents,
                                         available=quote.available, source=quote.source + '+visible')
                    return quote
                quote = _quote_from_seo(data, ref.sku)
                if quote:
                    dom = await _visible_widget_quote(page, ref)
                    if dom and dom.regular_cents is not None and dom.card_cents is not None:
                        return Quote(quote.name, dom.regular_cents, dom.card_cents,
                                     available=quote.available, source=dom.source)
                    return quote
            api_blocked = result.get('status') in _BLOCKED_CODES

        dom = await _visible_widget_quote(page, ref)
        if dom:
            return dom
        scripts = await page.locator('script[type="application/ld+json"]').all_text_contents()
        quote = parse_json_ld(scripts, ref.sku)
        if quote:
            return quote
        if api_blocked:
            raise ScrapeError('blocked', 'Ozon отклонил JSON API и не показал проверяемую цену')
        raise ScrapeError('no_price', 'Ozon: цена отсутствует в JSON витрины и разметке карточки')
    finally:
        # A reusable background page must not accumulate old listeners. Otherwise
        # every hourly check would spawn duplicate response-parsing tasks.
        page.remove_listener('response', on_response)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if worker_page is None:
            await page.close()


async def _fetch_persistent(ref: ProductRef, timeout_ms: int) -> Quote:
    """Visible local Chrome with a separate persistent profile, not a CAPTCHA bypass."""
    async with _PROFILE_LOCK:
        _PROFILE.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as pw:
            settings = {'headless': False, 'locale': 'ru-RU',
                        'viewport': {'width': 1365, 'height': 860}}
            connected_browser = None
            try:
                cdp = os.getenv('PRICE_TRACKER_CDP_URL', '').strip()
                if cdp:
                    # Attach to the ordinary Chrome opened by the user with debugging
                    # enabled; no fresh browser profile and no separate auth state.
                    from urllib.parse import urlsplit as split_cdp
                    parts = split_cdp(cdp)
                    if (parts.scheme != 'http' or
                            parts.hostname not in ('localhost', '127.0.0.1', '::1') or
                            not parts.port or parts.username or parts.password or
                            (parts.path not in ('', '/') or parts.query or parts.fragment)):
                        raise ScrapeError('browser', 'PRICE_TRACKER_CDP_URL должен указывать на локальный Chrome, например http://localhost:9222')
                    # Chrome on macOS may listen on IPv6 ::1 only. Try both
                    # local loopbacks instead of silently failing with a generic
                    # "cannot open Chrome" error. No remote hosts are permitted.
                    endpoints = [cdp, f'http://127.0.0.1:{parts.port}',
                                 f'http://[::1]:{parts.port}']
                    cdp_error = None
                    for endpoint in dict.fromkeys(endpoints):
                        try:
                            connected_browser = await pw.chromium.connect_over_cdp(endpoint, timeout=3500)
                            break
                        except PlaywrightError as exc:
                            cdp_error = exc
                    if connected_browser is None:
                        raise ScrapeError('browser',
                                          f'Нет подключения к Chrome CDP на порту {parts.port} (IPv4/IPv6). '
                                          f'Проверьте: curl http://localhost:{parts.port}/json/version. '
                                          f'Причина: {str(cdp_error).splitlines()[0][:180] if cdp_error else "неизвестна"}') from cdp_error
                    if not connected_browser.contexts:
                        raise ScrapeError('browser', 'Chrome CDP не вернул обычный контекст')
                    context = connected_browser.contexts[0]
                else:
                    try:
                        context = await pw.chromium.launch_persistent_context(
                            str(_PROFILE), channel='chrome', **settings)
                    except PlaywrightError as exc:
                        if 'Executable doesn\'t exist' not in str(exc) and 'not found' not in str(exc).lower():
                            raise
                        context = await pw.chromium.launch_persistent_context(str(_PROFILE), **settings)
            except PlaywrightError as exc:
                raise ScrapeError('browser', 'Не удалось открыть Chrome. Закройте другие окна Price Tracker Chrome '
                                  'или проверьте установленный браузер') from exc
            try:
                # Important: a successfully opened product tab may be readable
                # even when a brand-new navigation would return HTTP 403.
                if connected_browser is not None:
                    for existing_page in context.pages:
                        if _is_matching_open_tab(existing_page.url, ref.sku):
                            quote = await _read_existing_tab(existing_page, ref)
                            if quote:
                                return quote
                if connected_browser is not None:
                    from .cdp_worker import ozon_background_page
                    async with ozon_background_page(connected_browser, context) as worker:
                        return await _read_page(context, ref, timeout_ms, headed=True,
                                                worker_page=worker)
                return await _read_page(context, ref, timeout_ms, headed=True)
            except PlaywrightError as exc:
                raise ScrapeError('browser', f'Ozon: ошибка Chrome: {type(exc).__name__}') from exc
            finally:
                if connected_browser is not None:
                    await connected_browser.close()  # Disconnect, leave the user's Chrome running.
                else:
                    await context.close()


async def fetch_ozon(ref: ProductRef, headless: bool = True, timeout_ms: int = 30000) -> Quote:
    await _OZON_GATE.wait()
    if not headless:
        return await _fetch_persistent(ref, timeout_ms)
    # Fast path preserved: existing background operation, then a real local Chrome
    # profile if it gets blocked or the storefront schema is missing.
    initial_error: ScrapeError | None = None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(locale='ru-RU', viewport={'width': 1365, 'height': 860})
                try:
                    return await _read_page(ctx, ref, timeout_ms, headed=False)
                finally:
                    await ctx.close()
            finally:
                await browser.close()
    except ScrapeError as exc:
        if exc.code not in ('blocked', 'no_price', 'not_found'):
            raise
        initial_error = exc
    except PlaywrightError:
        initial_error = ScrapeError('browser', 'Headless Chromium недоступен')
    try:
        return await _fetch_persistent(ref, timeout_ms)
    except ScrapeError as exc:
        if exc.code == 'blocked' and initial_error is not None:
            raise ScrapeError('blocked', f'Ozon: HTTP 403 и в отдельном Chrome-профиле. '
                              'Если карточка не открывается даже в появившемся Chrome, '
                              'доступ ограничен площадкой/сетью.') from exc
        raise
