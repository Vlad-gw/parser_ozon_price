"""Single-process check orchestration, observed history and persistent alert outbox.

The price parser is intentionally not reimplemented here. Invalid/blocked responses
never become zero-price events. A queued notification survives application restarts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta, timezone

from sqlalchemy import select

from . import config
from .database import AlertEvent, Product, PricePoint, SessionLocal, utc_now
from .analytics import analytics_for_product
from .parsers import ProductRef, ScrapeError, fetch_ozon
from .telegram_bot import send_message, product_keyboard, rub

log = logging.getLogger(__name__)
_product_locks: dict[int, asyncio.Lock] = {}
_browser_lock = asyncio.Lock()  # prevent Telegram and scheduler navigating the same CDP page
_scan_lock = asyncio.Lock()
_alert_lock = asyncio.Lock()


def product_dict(p: Product) -> dict:
    with SessionLocal() as db:
        statistics = analytics_for_product(db, p)
    return {
        'id': p.id, 'marketplace': p.marketplace, 'sku': p.sku, 'url': p.url,
        'name': p.name, 'image_url': p.image_url, 'price_kind': p.price_kind,
        'target_price': p.target_cents / 100,
        'current_price': p.current_cents / 100 if p.current_cents is not None else None,
        'current_regular_price': p.current_regular_cents / 100 if p.current_regular_cents is not None else None,
        'current_card_price': p.current_card_cents / 100 if p.current_card_cents is not None else None,
        'price_verified': p.price_verified, 'last_source': p.last_source,
        'available': p.available, 'paused': p.paused, 'failure_count': p.failure_count,
        'last_error': p.last_error, 'last_notify_error': p.last_notify_error,
        'last_checked_at': p.last_checked_at.isoformat() if p.last_checked_at else None,
        'last_success_at': p.last_success_at.isoformat() if p.last_success_at else None,
        'created_at': p.created_at.isoformat() if p.created_at else None,
        'analytics': statistics,
    }


async def fetch_quote(ref: ProductRef):
    if ref.marketplace != 'ozon':
        raise ValueError('Поддерживается только Ozon')
    return await fetch_ozon(ref, headless=config.OZON_HEADLESS, timeout_ms=config.OZON_TIMEOUT_MS)


def _cancel_unsent(db, product_id: int) -> None:
    for event in db.scalars(select(AlertEvent).where(
        AlertEvent.product_id == product_id,
        AlertEvent.status.in_(['pending', 'sending'])
    )):
        event.status = 'cancelled'


def _verified_source(source: str) -> bool:
    """Unlabeled SEO/JSON-LD offer cannot prove regular-vs-card classification."""
    return bool(source) and 'json-ld' not in source.lower() and 'visible-unclassified' not in source.lower()


async def send_telegram(p: Product, price_cents: int) -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise RuntimeError('Telegram не настроен: заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID')
    with SessionLocal() as db:
        stats = analytics_for_product(db, p)['selected']
    movement = ''
    if stats:
        delta, pct = stats['change_amount'], stats['change_percent']
        sign = '+' if delta > 0 else '−' if delta < 0 else ''
        movement = (f'\n📈 От первой цены: {sign}{rub(round(abs(delta) * 100))}'
                    + (f' ({pct:+.1f}%)' if pct is not None else ''))
    text = (f'🔔 Цена достигла цели!\n{p.name[:200]}\n'
            f'Площадка: Ozon · {"по карте OZON" if p.price_kind == "card" else "с другими банками"}\n'
            f'Сейчас: {rub(price_cents)}\nЦель: {rub(p.target_cents)}\n'
            f'\nПо карте OZON: {rub(p.current_card_cents) if p.current_card_cents is not None else "нет данных"}'
            f'\nС другими банками: {rub(p.current_regular_cents) if p.current_regular_cents is not None else "нет данных"}'
            f'{movement}\n{p.url}')
    await send_message(text, keyboard=product_keyboard(p))


async def dispatch_pending_alerts(product_id: int | None = None) -> None:
    """Claim in SQLite, then send outside transaction. Retry network failures with backoff.

    An ambiguous crash after Telegram accepted the message but before commit can
    still create a duplicate. Telegram Bot API offers no idempotency key.
    """
    async with _alert_lock:
        now = utc_now()
        with SessionLocal() as db:
            query = select(AlertEvent.id).where(AlertEvent.status.in_(['pending', 'sending']))
            if product_id is not None:
                query = query.where(AlertEvent.product_id == product_id)
            ids = list(db.scalars(query.order_by(AlertEvent.id).limit(30)))

        for alert_id in ids:
            with SessionLocal() as db:
                event = db.get(AlertEvent, alert_id)
                if not event or event.status not in ('pending', 'sending'):
                    continue
                if event.next_attempt_at and _utc(event.next_attempt_at) > utc_now():
                    continue
                if event.status == 'sending' and event.claimed_at and (utc_now()-_utc(event.claimed_at)) < timedelta(minutes=5):
                    continue
                p = db.get(Product, event.product_id)
                fresh = (p and p.last_success_at and
                         (utc_now() - _utc(p.last_success_at)) < timedelta(minutes=max(20, config.CHECK_INTERVAL_MIN*2)))
                if not p or p.paused or not p.notified_below or p.available is False or \
                   p.last_error or not p.price_verified or not fresh or \
                   event.episode != p.alert_sequence or event.price_cents > p.target_cents:
                    # Do not deliver an old price after the item recovered or was paused.
                    if not p or p.paused or not p.notified_below or p.available is False or \
                       event.episode != (p.alert_sequence if p else -1) or event.price_cents > (p.target_cents if p else 0):
                        event.status = 'cancelled'
                        db.commit()
                    continue
                event.status = 'sending'
                event.claimed_at = utc_now()
                event.attempts += 1
                db.commit()
                await_price = event.price_cents
                product_ref = p
                attempts = event.attempts

            try:
                await send_telegram(product_ref, await_price)
            except Exception as exc:
                # Network uncertainty: retry later, capped exponential backoff.
                log.warning('Telegram alert #%s failed: %s', alert_id, type(exc).__name__)
                with SessionLocal() as db:
                    e = db.get(AlertEvent, alert_id)
                    p = db.get(Product, e.product_id) if e else None
                    if e:
                        e.status = 'pending'
                        e.last_error = f'{type(exc).__name__}: {str(exc)}'[:480]
                        e.next_attempt_at = utc_now() + timedelta(minutes=min(60, 2 ** min(attempts, 5)))
                    if p:
                        p.last_notify_error = 'Не удалось доставить Telegram-уведомление; повторим позже'
                    db.commit()
            else:
                with SessionLocal() as db:
                    e = db.get(AlertEvent, alert_id)
                    p = db.get(Product, e.product_id) if e else None
                    if e:
                        e.status = 'sent'
                        e.sent_at = utc_now()
                        e.last_error = None
                    if p:
                        p.last_notify_error = None
                    db.commit()


def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


async def check_product(product_id: int, *, force: bool = True) -> dict | None:
    lock = _product_locks.setdefault(product_id, asyncio.Lock())
    async with lock:
        with SessionLocal() as db:
            p = db.get(Product, product_id)
            if p is None or p.marketplace != 'ozon':
                return None
            if p.paused and not force:
                return product_dict(p)
            ref = ProductRef(p.marketplace, p.sku, p.url)
        try:
            # Playwright's shared CDP page must not be navigated by parallel tasks.
            async with _browser_lock:
                quote = await fetch_quote(ref)
            now = utc_now()
            with SessionLocal() as db:
                p = db.get(Product, product_id)
                if p is None:
                    return None
                price = quote.card_cents if p.price_kind == 'card' else quote.regular_cents
                if quote.available is not False and price is None:
                    raise ScrapeError('no_price', f'Ozon: выбранная цена ({p.price_kind}) отсутствует')
                verified = _verified_source(quote.source)
                p.last_checked_at = now
                p.last_success_at = now
                p.last_error = None
                p.failure_count = 0
                p.last_source = quote.source[:100]
                p.price_verified = verified
                p.name = quote.name[:500]
                p.available = quote.available
                p.current_cents = price
                p.current_regular_cents = quote.regular_cents if quote.available is not False else None
                p.current_card_cents = quote.card_cents if quote.available is not False else None
                if quote.image_url:
                    p.image_url = quote.image_url[:800]
                if price is not None:
                    db.add(PricePoint(product_id=p.id, price_cents=price,
                                      regular_cents=quote.regular_cents, card_cents=quote.card_cents,
                                      price_verified=verified, price_source=quote.source[:100], checked_at=now))
                if p.paused or quote.available is False or (verified and price is not None and price > p.target_cents):
                    p.notified_below = False
                    p.last_notify_error = None
                    _cancel_unsent(db, p.id)
                elif price is not None and price <= p.target_cents and verified and not p.notified_below:
                    p.notified_below = True
                    p.alert_sequence += 1
                    db.add(AlertEvent(product_id=p.id, episode=p.alert_sequence,
                                      price_cents=price, target_cents=p.target_cents, status='pending'))
                db.commit()
                result = product_dict(p)
            await dispatch_pending_alerts(product_id)
            return result
        except ScrapeError as exc:
            error = f'{exc.code}: {str(exc)}'[:500]
            log.warning('Price check failed for Ozon #%s: %s', ref.sku, error)
        except Exception as exc:
            error = f'unexpected: {type(exc).__name__}'
            log.exception('Unexpected check error %s', product_id)
        with SessionLocal() as db:
            p = db.get(Product, product_id)
            if p is None:
                return None
            p.last_checked_at = utc_now()
            p.last_error = error
            p.failure_count += 1
            db.commit()
            return product_dict(p)


async def check_all() -> None:
    # Skip overlapping hourly jobs and overlapping Telegram /prices requests.
    if _scan_lock.locked():
        # A price-check request arriving during a scheduled scan waits for it
        # rather than showing an older snapshot as if a new refresh completed.
        async with _scan_lock:
            return
    async with _scan_lock:
        with SessionLocal() as db:
            ids = list(db.scalars(select(Product.id).where(
                Product.marketplace == 'ozon', Product.paused.is_(False)).order_by(Product.id)))
        for pid in ids:
            try:
                await check_product(pid, force=False)
            except Exception:
                log.exception('Scheduled check failure for product #%s', pid)
            await asyncio.sleep(1)
        await dispatch_pending_alerts()
