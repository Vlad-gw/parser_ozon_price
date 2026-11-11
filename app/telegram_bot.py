"""Telegram long polling, threshold alerts and prices only on user request.

Only TELEGRAM_CHAT_ID can issue commands. The live-price button always initiates a
new check_product(); if Ozon blocks it, the bot labels any stored price as old.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import select

from . import config
from .database import Product, SessionLocal
from .analytics import analytics_for_product

log = logging.getLogger(__name__)
_pending: set[asyncio.Task] = set()


def configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def is_authorized(chat_id: object) -> bool:
    return configured() and str(chat_id) == str(config.TELEGRAM_CHAT_ID)


def rub(cents: int) -> str:
    return f'{cents / 100:,.2f}'.replace(',', ' ').replace('.', ',') + ' ₽'


def time_label(value: datetime | None) -> str:
    if value is None:
        return 'ещё не проверено'
    # SQLite can return naive datetimes; the project stores them in UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(config.APP_TIMEZONE)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return value.astimezone(zone).strftime('%d.%m.%Y %H:%M')


def product_keyboard(p: Product) -> dict:
    return {'inline_keyboard': [[
        {'text': f'🔄 Проверить цену #{p.id}', 'callback_data': f'check:{p.id}'},
        {'text': '🛒 Ozon', 'url': p.url},
    ], [
        {'text': '📈 Аналитика', 'callback_data': f'stats:{p.id}'},
        {'text': '▶ Возобновить' if p.paused else '⏸ Пауза', 'callback_data': f'pause:{p.id}'},
    ]]}


def _format_signed_cents(amount_rub: float) -> str:
    sign = '+' if amount_rub > 0 else '−' if amount_rub < 0 else ''
    return sign + rub(round(abs(amount_rub) * 100))


def _format_change(info: dict) -> str:
    diff = info['change_amount']
    sign = '+' if diff > 0 else '−' if diff < 0 else ''
    percent = info['change_percent']
    pct_text = (f' ({sign}{abs(percent):.1f}%)' if percent is not None else '')
    return f'{_format_signed_cents(diff)}{pct_text}'


def _stats_for(p: Product) -> dict:
    with SessionLocal() as db:
        return analytics_for_product(db, p)


def _compact_stats(p: Product) -> str:
    current = _stats_for(p)['selected']
    if not current:
        return '📈 Динамика: пока нет замеров'
    return (f'📈 От первой цены ({rub(round(current["initial_price"] * 100))}): '
            f'{_format_change(current)}\n'
            f'📉 Мин.: {rub(round(current["min_price"] * 100))} · '
            f'Макс.: {rub(round(current["max_price"] * 100))} '
            f'· замеров: {current["observations"]}')


def _expanded_stats(kind: str, info: dict | None) -> str:
    title = 'По карте OZON' if kind == 'card' else 'С другими банками'
    if not info:
        return f'• {title}: данных пока нет'
    out = [f'• {title}',
           f'Первая цена: {rub(round(info["initial_price"] * 100))} ({time_label(datetime.fromisoformat(info["initial_at"]))})',
           f'Последняя: {rub(round(info["latest_price"] * 100))}',
           f'Изменение: {_format_change(info)}',
           f'Минимум: {rub(round(info["min_price"] * 100))} · Максимум: {rub(round(info["max_price"] * 100))}',
           f'Средняя по замерам: {rub(round(info["average_price"] * 100))} · Замеров: {info["observations"]}']
    for window, label in [('change_24h', 'За 24 часа'), ('change_7d', 'За 7 дней')]:
        if info[window] is not None:
            pct = info[window]['percent']
            pct_str = f' ({pct:+.1f}%)' if pct is not None else ''
            out.append(f'{label}: {_format_signed_cents(info[window]["amount"])}{pct_str}')
    if info.get('time_weighted_average_price') is not None:
        out.append(f'Средняя с учётом времени*: {rub(round(info["time_weighted_average_price"]*100))}')
    if info.get('min_at'):
        out.append(f'Самая низкая цена: {time_label(datetime.fromisoformat(info["min_at"]))}')
    if info.get('max_discount_from_initial') is not None:
        out.append(f'Макс. снижение от первой: {rub(round(info["max_discount_from_initial"]*100))}')
    if info.get('below_target_estimated_hours') is not None:
        out.append(f'Примерно часов по цене ≤ цели*: {info["below_target_estimated_hours"]}')
    return '\n'.join(out)


def product_line(p: Product) -> str:
    title = p.name[:120]
    kind = 'по карте OZON' if p.price_kind == 'card' else 'с другими банками'
    if p.paused:
        current = '⏸ Отслеживание на паузе'
        freshness = f'Последняя проверка: {time_label(p.last_success_at)}'
    elif p.last_error:
        current = (f'⚠️ Последняя известная: {rub(p.current_cents)}' if p.current_cents is not None
                   else '⚠️ Цена не получена')
        freshness = f'Ошибка новой проверки: {p.last_error[:115]}'
    elif p.available is False:
        current = '🚫 Нет в наличии'
        freshness = f'Проверено: {time_label(p.last_success_at)}'
    elif p.current_cents is not None:
        current = f'💰 Цена: {rub(p.current_cents)}'
        freshness = f'Проверено: {time_label(p.last_success_at)}'
    else:
        current = '❔ Цена пока неизвестна'
        freshness = f'Последняя успешная проверка: {time_label(p.last_success_at)}'

    both = (f'🏦 По карте OZON: {rub(p.current_card_cents) if p.current_card_cents is not None else "нет данных"}\n'
            f'💳 С другими банками: {rub(p.current_regular_cents) if p.current_regular_cents is not None else "нет данных"}')
    source_note = ('\n⚠️ Тип цены не подтверждён: JSON-LD/неподписанная цена; автоматический сигнал не отправляется.'
                   if p.current_cents is not None and not p.price_verified else '')
    return (f'#{p.id} · {title}\n{current} ({kind})\n'
            f'{both}\n🎯 Цель: {rub(p.target_cents)}\n'
            f'{_compact_stats(p)}\n{freshness}{source_note}')


async def api(method: str, payload: dict) -> object:
    """Use the official Bot API; never print the token or Telegram request URL."""
    if not configured():
        raise RuntimeError('Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env')
    url = f'https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}'
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=10)) as client:
            response = await client.post(url, json=payload)
            # The error description from Telegram does not include our token.
            try:
                data = response.json()
            except ValueError:
                raise RuntimeError(f'Telegram API {method}: ответ не JSON') from None
            if not isinstance(data, dict) or not data.get('ok'):
                detail = str(data.get('description', 'неизвестная ошибка'))[:180] if isinstance(data, dict) else 'неизвестная ошибка'
                raise RuntimeError(f'Telegram API {method}: HTTP {response.status_code}; {detail}')
            return data.get('result')
    except httpx.HTTPError:
        # HTTPX exceptions may contain the URL with the secret Bot token.
        raise RuntimeError(f'Telegram API {method}: сетевая ошибка') from None


async def send_message(text: str, *, keyboard: dict | None = None) -> None:
    payload = {
        'chat_id': config.TELEGRAM_CHAT_ID,
        'text': text[:4096],
        'link_preview_options': {'is_disabled': True},
    }
    if keyboard:
        payload['reply_markup'] = keyboard
    await api('sendMessage', payload)


def ozon_products() -> list[Product]:
    # Session is closed before network awaits. SQLAlchemy expire_on_commit=False.
    with SessionLocal() as db:
        return list(db.scalars(select(Product).where(Product.marketplace == 'ozon').order_by(Product.id)))


async def send_summary() -> None:
    """Send a summary ONLY after a user command or inline-button request."""
    products = ozon_products()
    if not products:
        await send_message('📦 Пока нет товаров Ozon. Добавь первый товар в веб-интерфейсе.')
        return
    for first in range(0, len(products), 8):
        batch = products[first:first + 8]
        body = '📊 Ozon Price Tracker — проверка по запросу\n\n' + '\n\n'.join(product_line(p) for p in batch)
        buttons = [[
            {'text': f'🔄 Проверить #{p.id}', 'callback_data': f'check:{p.id}'},
            {'text': f'🛒 Ozon #{p.id}', 'url': p.url},
            {'text': f'📈 #{p.id}', 'callback_data': f'stats:{p.id}'},
        ] for p in batch]
        await send_message(body, keyboard={'inline_keyboard': buttons})


async def _send_stats(product_id: int) -> None:
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None or p.marketplace != 'ozon':
            await send_message('Товар не найден или удалён.')
            return
        stats = analytics_for_product(db, p)
        text = (f'📈 Аналитика Ozon #{p.id}\n{p.name[:175]}\n'
                f'Выбранная цена для уведомлений: '
                f'{"по карте OZON" if p.price_kind == "card" else "с другими банками"}\n\n'
                + _expanded_stats('card', stats['card']) + '\n\n'
                + _expanded_stats('regular', stats['regular']) + '\n\n'
                + 'ℹ️ Изменения считаются с первой успешной проверки каждого типа цены. '
                  'Средняя по замерам не учитывает длительность цены. *Показатели по времени приблизительные: считаем цену неизменной между проверками.\n'
                + (f'⚠️ Последняя проверка с ошибкой: {p.last_error[:100]}' if p.last_error else
                   f'Последняя успешная проверка: {time_label(p.last_success_at)}'))
        keyboard = product_keyboard(p)
    await send_message(text, keyboard=keyboard)


async def _send_all_stats() -> None:
    products = ozon_products()
    if not products:
        await send_message('Пока нет товаров для аналитики.')
        return
    for p in products:
        await _send_stats(p.id)


async def _refresh_and_reply(product_id: int) -> None:
    from .service import check_product
    try:
        current = await check_product(product_id)
        if current is None:
            await send_message('Товар удалён из отслеживания.')
            return
        # Query the DB again to use the same rendering as scheduled digests.
        with SessionLocal() as db:
            p = db.get(Product, product_id)
            if p is None or p.marketplace != 'ozon':
                await send_message('Товар удалён из отслеживания.')
                return
            text = ('🔄 Результат проверки прямо сейчас\n\n' + product_line(p))
            keyboard = product_keyboard(p)
        await send_message(text, keyboard=keyboard)
    except Exception:
        log.exception('Unable to complete Telegram live check for product %s', product_id)
        try:
            await send_message(f'⚠️ Не удалось выполнить проверку товара #{product_id}. Проверь журнал сервера.')
        except RuntimeError:
            pass


async def _refresh_all_and_reply() -> None:
    from .service import check_all
    try:
        await check_all()
        await send_summary()
    except Exception:
        log.exception('Telegram full refresh failed')


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def stop_pending() -> None:
    for task in list(_pending):
        task.cancel()
    if _pending:
        await asyncio.gather(*_pending, return_exceptions=True)


async def handle_update(update: dict) -> None:
    """Ignores all messages from chats other than configured TELEGRAM_CHAT_ID."""
    cb = update.get('callback_query')
    if isinstance(cb, dict):
        chat_id = ((cb.get('message') or {}).get('chat') or {}).get('id')
        if not is_authorized(chat_id):
            # Do not reveal prices to someone else who can see a forwarded button.
            if cb.get('id'):
                try:
                    await api('answerCallbackQuery', {'callback_query_id': cb['id'], 'text': 'Нет доступа', 'show_alert': True})
                except RuntimeError:
                    pass
            return
        action = str(cb.get('data') or '')
        if action.startswith('check:') and action[6:].isdigit():
            product_id = int(action[6:])
            await api('answerCallbackQuery', {'callback_query_id': cb['id'], 'text': 'Проверяю цену Ozon…'})
            _spawn(_refresh_and_reply(product_id))
        elif action.startswith('pause:') and action[6:].isdigit():
            pid = int(action[6:])
            from .service import _cancel_unsent
            with SessionLocal() as db:
                product = db.get(Product, pid)
                if product is None or product.marketplace != 'ozon':
                    label = 'Товар не найден'
                else:
                    product.paused = not product.paused
                    if product.paused:
                        _cancel_unsent(db, pid)
                        product.notified_below = False
                    db.commit()
                    label = '⏸ Приостановлено' if product.paused else '▶ Отслеживание включено'
            await api('answerCallbackQuery', {'callback_query_id': cb['id'], 'text': label})
            _spawn(_send_stats(pid))
        elif action.startswith('stats:') and action[6:].isdigit():
            await api('answerCallbackQuery', {'callback_query_id': cb['id'], 'text': 'Показываю аналитику…'})
            _spawn(_send_stats(int(action[6:])))
        elif action == 'prices':
            await api('answerCallbackQuery', {'callback_query_id': cb['id']})
            _spawn(_refresh_all_and_reply())
        else:
            await api('answerCallbackQuery', {'callback_query_id': cb['id'], 'text': 'Неизвестная кнопка'})
        return

    message = update.get('message')
    if not isinstance(message, dict) or not is_authorized((message.get('chat') or {}).get('id')):
        return
    command = str(message.get('text') or '').strip().split(' ', 1)[0].split('@', 1)[0].lower()
    if command in ('/start', '/help'):
        await send_message(
            '👋 Ozon Price Tracker подключён!\n\n'
            '🔔 Автоматические сообщения — только при достижении целевой цены.\n'
            '📊 Текущая цена — только по вашему запросу.\n\n'
            '/prices — проверить все товары и показать актуальные цены\n'
            '/refresh — то же самое: проверить все товары заново\n'
            '/stats — статистика изменений цен (без новой проверки)\n'
            '/test — проверить доставку тестового сообщения',
            keyboard={'inline_keyboard': [[{'text': '🔄 Проверить цены сейчас', 'callback_data': 'prices'}]]},
        )
    elif command in ('/prices', '/price'):
        await send_message('🔄 Проверяю цены Ozon. Результаты пришлю после проверки.')
        _spawn(_refresh_all_and_reply())
    elif command == '/refresh':
        await send_message('🔄 Запустил обновление всех товаров. Результаты придут отдельным сообщением.')
        _spawn(_refresh_all_and_reply())
    elif command == '/stats':
        _spawn(_send_all_stats())
    elif command == '/test':
        products = ozon_products()
        keyboard = product_keyboard(products[0]) if products else None
        await send_message('🧪 ТЕСТОВОЕ уведомление!\n'
                           'Telegram-кнопки и отправка сообщений работают.\n'
                           'Это НЕ означает реального снижения цены.', keyboard=keyboard)


async def poll_updates() -> None:
    """Run exactly once per server, not with uvicorn --workers or --reload."""
    if not configured():
        return
    offset: int | None = None
    log.info('Telegram bot polling started')
    while True:
        payload = {'timeout': 20, 'allowed_updates': ['message', 'callback_query']}
        if offset is not None:
            payload['offset'] = offset
        try:
            updates = await api('getUpdates', payload)
            if not isinstance(updates, list):
                raise RuntimeError('getUpdates вернул не массив')
            for update in updates:
                if not isinstance(update, dict):
                    continue
                update_id = update.get('update_id')
                if isinstance(update_id, int):
                    offset = max(offset or 0, update_id + 1)
                try:
                    await handle_update(update)
                except Exception as exc:
                    log.warning('Telegram update handling failed: %s', type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Telegram polling error: %s (retry after 5 s)', str(exc)[:180])
            await asyncio.sleep(5)
