"""Offline Telegram workflow tests: no Chrome, Telegram or internet required."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, service, telegram_bot as tg
from app.database import Base, Product
from app.parsers.common import Quote


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "bot.sqlite3"}')
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(service, 'SessionLocal', maker)
    monkeypatch.setattr(tg, 'SessionLocal', maker)
    monkeypatch.setattr(config, 'TELEGRAM_BOT_TOKEN', 'offline-test-token')
    monkeypatch.setattr(config, 'TELEGRAM_CHAT_ID', '123456789')
    yield maker
    engine.dispose()


def add_product(maker, *, price=88400, target=55000):
    with maker() as session:
        p = Product(marketplace='ozon', sku='2190590687',
                    url='https://www.ozon.ru/product/2190590687/',
                    name='Чехол для мотоцикла', price_kind='regular',
                    target_cents=target, current_cents=price,
                    available=True, last_success_at=datetime.now(timezone.utc),
                    notified_below=False)
        session.add(p)
        session.commit()
        return p.id


def test_price_message_has_live_button_and_timestamp(db):
    product_id = add_product(db)
    items = tg.ozon_products()
    assert len(items) == 1
    text = tg.product_line(items[0])
    assert '884,00 ₽' in text
    assert '550,00 ₽' in text
    assert 'Проверено' in text
    kb = tg.product_keyboard(items[0])
    assert kb['inline_keyboard'][0][0]['callback_data'] == f'check:{product_id}'


def test_threshold_crossing_sends_once_then_reset(db, monkeypatch):
    product_id = add_product(db, target=55000)
    quotes = iter([54000, 53000, 60000, 52000])
    sent = []

    async def fake_fetch(_ref):
        return Quote('Чехол для мотоцикла', regular_cents=next(quotes),
                     available=True, source='test')

    async def fake_send(text, *, keyboard=None):
        sent.append((text, keyboard))

    monkeypatch.setattr(service, 'fetch_quote', fake_fetch)
    monkeypatch.setattr(service, 'send_message', fake_send)

    async def scenario():
        for _ in range(4):
            response = await service.check_product(product_id)
            assert response['last_error'] is None

    asyncio.run(scenario())
    assert len(sent) == 2, 'Notify on first crossing, suppress repeats, rearm when above'
    assert '540,00' in sent[0][0]
    assert '520,00' in sent[1][0]
    assert sent[0][1]['inline_keyboard'][0][0]['callback_data'] == f'check:{product_id}'


def test_test_command_and_prices_command(db, monkeypatch):
    add_product(db)
    sent = []

    async def fake_send(text, *, keyboard=None):
        sent.append((text, keyboard))

    async def fake_fetch(_ref):
        return Quote('Чехол', regular_cents=88400, available=True, source='test')

    monkeypatch.setattr(tg, 'send_message', fake_send)
    monkeypatch.setattr(service, 'fetch_quote', fake_fetch)

    async def scenario():
        await tg.handle_update({'message': {'chat': {'id': 123456789}, 'text': '/test'}})
        await tg.handle_update({'message': {'chat': {'id': 123456789}, 'text': '/prices'}})
        await asyncio.gather(*list(tg._pending))

    asyncio.run(scenario())
    assert len(sent) == 3
    assert 'ТЕСТОВОЕ' in sent[0][0]
    assert 'Проверяю цены' in sent[1][0]
    assert '884,00' in sent[2][0]
    assert sent[2][1]['inline_keyboard'][0][0]['callback_data'] == 'check:1'


def test_live_callback_rechecks_the_price(db, monkeypatch):
    product_id = add_product(db)
    sent = []
    requested = []

    async def fake_send(text, *, keyboard=None):
        sent.append(text)

    async def fake_api(method, data):
        requested.append((method, data))

    async def fake_fetch(_ref):
        return Quote('Чехол для мотоцикла', regular_cents=77700,
                     available=True, source='test')

    monkeypatch.setattr(tg, 'send_message', fake_send)
    monkeypatch.setattr(tg, 'api', fake_api)
    monkeypatch.setattr(service, 'fetch_quote', fake_fetch)

    async def scenario():
        await tg.handle_update({'callback_query': {
            'id': 'cb1', 'data': f'check:{product_id}',
            'message': {'chat': {'id': 123456789}},
        }})
        await asyncio.gather(*list(tg._pending))

    asyncio.run(scenario())
    assert requested[0][0] == 'answerCallbackQuery'
    assert any('777,00' in message and 'Результат проверки' in message for message in sent)


def test_failure_never_labels_cached_price_current(db, monkeypatch):
    product_id = add_product(db)
    sent = []

    async def fake_send(text, *, keyboard=None):
        sent.append(text)

    async def fake_fetch(_ref):
        from app.parsers.common import ScrapeError
        raise ScrapeError('blocked', 'Ozon HTTP 403')

    monkeypatch.setattr(tg, 'send_message', fake_send)
    monkeypatch.setattr(service, 'fetch_quote', fake_fetch)
    asyncio.run(tg._refresh_and_reply(product_id))
    assert 'Последняя известная' in sent[0]
    assert '884,00' in sent[0]
    assert 'Ошибка новой проверки' in sent[0]
    assert 'Цена:' not in sent[0]


def test_unauthorized_chat_ignored(db, monkeypatch):
    sent = []

    async def fake_send(text, *, keyboard=None):
        sent.append(text)

    monkeypatch.setattr(tg, 'send_message', fake_send)
    asyncio.run(tg.handle_update({'message': {'chat': {'id': 987654321}, 'text': '/prices'}}))
    assert sent == []


def test_start_command_replies_and_shows_report_interval(db, monkeypatch):
    sent = []
    async def fake_send(text, *, keyboard=None):
        sent.append((text, keyboard))
    monkeypatch.setattr(tg, 'send_message', fake_send)
    asyncio.run(tg.handle_update({'message': {'chat': {'id': 123456789}, 'text': '/start'}}))
    assert len(sent) == 1
    assert '/prices' in sent[0][0]
    assert 'только при достижении целевой цены' in sent[0][0]
    assert sent[0][1]['inline_keyboard'][0][0]['callback_data'] == 'prices'


def test_equal_threshold_sends_once_then_rearms_only_above(db, monkeypatch):
    pid = add_product(db, target=55000)
    amounts = iter([55000, 54000, 55000, 55100, 55000])
    alerts = []

    async def fetch(_):
        return Quote('Чехол', regular_cents=next(amounts), available=True, source='test')

    async def send(text, *, keyboard=None):
        alerts.append(text)

    monkeypatch.setattr(service, 'fetch_quote', fetch)
    monkeypatch.setattr(service, 'send_message', send)

    async def scenario():
        for _ in range(5):
            result = await service.check_product(pid)
            assert result['last_error'] is None

    asyncio.run(scenario())
    assert len(alerts) == 2
    assert all('550,00' in msg for msg in alerts)


def test_prices_command_requests_new_quotes(db, monkeypatch):
    pid = add_product(db)
    sent = []
    calls = []

    async def send(text, *, keyboard=None):
        sent.append(text)

    async def fetch(_):
        calls.append('fetch')
        return Quote('Чехол', regular_cents=76600, available=True, source='test')

    monkeypatch.setattr(tg, 'send_message', send)
    monkeypatch.setattr(service, 'fetch_quote', fetch)

    async def scenario():
        await tg.handle_update({'message': {'chat': {'id': 123456789}, 'text': '/prices'}})
        await asyncio.gather(*list(tg._pending))

    asyncio.run(scenario())
    assert calls == ['fetch']
    assert any('766,00 ₽' in message for message in sent)
    assert not any('884,00 ₽' in message for message in sent)


def test_prices_callback_requests_new_quotes(db, monkeypatch):
    pid = add_product(db)
    results = []
    acks = []

    async def send(text, *, keyboard=None):
        results.append(text)

    async def answer(method, payload):
        acks.append(method)

    async def fetch(_):
        return Quote('Чехол', regular_cents=70700, available=True, source='test')

    monkeypatch.setattr(tg, 'api', answer)
    monkeypatch.setattr(tg, 'send_message', send)
    monkeypatch.setattr(service, 'fetch_quote', fetch)

    async def scenario():
        await tg.handle_update({'callback_query': {
            'id':'cb-prices', 'data':'prices', 'message':{'chat':{'id':123456789}},
        }})
        await asyncio.gather(*list(tg._pending))

    asyncio.run(scenario())
    assert acks == ['answerCallbackQuery']
    assert any('707,00 ₽' in msg for msg in results)


def test_scheduler_has_no_periodic_telegram_messages(monkeypatch, tmp_path):
    from app import main
    from app.database import Base
    engine = create_engine(f'sqlite:///{tmp_path / "jobs.sqlite3"}')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, 'init_db', lambda: None)
    monkeypatch.setattr(main.config, 'SCHEDULER_ENABLED', True)
    monkeypatch.setattr(main.config, 'TELEGRAM_POLLING_ENABLED', False)
    # Even if old .env had an interval, it must no longer schedule a digest.
    monkeypatch.setenv('TELEGRAM_REPORT_INTERVAL_HOURS', '3')
    from fastapi.testclient import TestClient
    with TestClient(main.app) as client:
        body = client.get('/api/health').json()
        assert body['scheduler_running'] is True
        assert body['telegram_reports_enabled'] is False
        assert (any(job.id == 'check_prices' for job in main.scheduler.get_jobs())
                if hasattr(main.scheduler, 'get_jobs') else 'check_prices' in main.scheduler.jobs)
        if hasattr(main.scheduler,'get_jobs'):
            assert all(job.id != 'telegram_price_report' for job in main.scheduler.get_jobs())
        else:
            assert 'telegram_price_report' not in main.scheduler.jobs
    engine.dispose()
