"""Regression tests for persistence, safe threshold events, authentication and pause."""
from __future__ import annotations
import asyncio
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main, service, telegram_bot as tg
from app.database import Base, Product, AlertEvent, utc_now
from app.parsers.common import Quote
from backup_db import backup_database

@pytest.fixture
def suite(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path/"p.sqlite3"}')
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (main, service, tg):
        monkeypatch.setattr(module, 'SessionLocal', maker)
    monkeypatch.setattr(main.config, 'SCHEDULER_ENABLED', False)
    monkeypatch.setattr(main.config, 'TELEGRAM_POLLING_ENABLED', False)
    monkeypatch.setattr(main.config, 'APP_PASSWORD', '')
    monkeypatch.setattr(main.config, 'TELEGRAM_BOT_TOKEN', 'offline-test')
    monkeypatch.setattr(main.config, 'TELEGRAM_CHAT_ID', '12345')
    with TestClient(main.app) as client:
        yield client, maker
    engine.dispose()

def test_pause_skips_scheduler_but_manual_refresh_works(suite, monkeypatch):
    client, maker = suite
    seen = []
    async def quote(_):
        seen.append(1)
        return Quote('Товар', 86000, 78000, True, 'test:mock')
    monkeypatch.setattr(service, 'fetch_quote', quote)
    r = client.post('/api/products', json={'link':'2190590687','target_price':500})
    assert r.status_code == 201
    pid = r.json()['id']
    assert client.patch(f'/api/products/{pid}/pause', json={'paused':True}).json()['paused']
    asyncio.run(service.check_all())
    assert len(seen) == 1
    assert client.post(f'/api/products/{pid}/check').status_code == 200
    assert len(seen) == 2
    assert client.patch(f'/api/products/{pid}/pause', json={'paused':False}).json()['paused'] is False
    asyncio.run(service.check_all())
    assert len(seen) == 3

def test_duplicate_prevention_outbox_and_retry(suite, monkeypatch):
    client, maker = suite
    seen=[]
    async def quote(_): return Quote('Товар', 50000, 49000, True, 'test:mock')
    async def send(text, *, keyboard=None):
        seen.append(text)
        if len(seen) == 1:
            raise RuntimeError('temporary failure')
    monkeypatch.setattr(service, 'fetch_quote', quote)
    monkeypatch.setattr(service, 'send_message', send)
    p=client.post('/api/products', json={'link':'2190590687','target_price':550}).json()
    with maker() as db:
        item=db.get(Product,p['id'])
        events=list(db.scalars(select(AlertEvent)))
        assert len(events)==1 and item.notified_below and events[0].status=='pending'
        events[0].next_attempt_at=utc_now()-timedelta(seconds=3)
        db.commit()
    asyncio.run(service.dispatch_pending_alerts())
    client.post(f'/api/products/{p["id"]}/check')
    with maker() as db:
        events=list(db.scalars(select(AlertEvent)))
        assert len(events)==1 and events[0].status=='sent'
    assert len(seen)==2

def test_unclassified_jsonld_price_does_not_trigger_alert(suite, monkeypatch):
    client, maker=suite
    messages=[]
    async def quote(_): return Quote('Товар Ozon', 50000, source='ozon:json-ld', available=True)
    async def send(text, *, keyboard=None): messages.append(text)
    monkeypatch.setattr(service,'fetch_quote',quote)
    monkeypatch.setattr(service,'send_message',send)
    p=client.post('/api/products',json={'link':'2190590687','target_price':600}).json()
    assert p['current_price']==500 and not p['price_verified']
    with maker() as db:
        assert list(db.scalars(select(AlertEvent)))==[]
    assert messages==[]

def test_remote_api_basic_auth(suite, monkeypatch):
    client,_=suite
    monkeypatch.setattr(main.config,'APP_PASSWORD','my-long-safe-password')
    assert client.get('/api/products').status_code==401
    assert client.get('/static/app.js').status_code==401
    assert client.get('/api/products', auth=('admin','bad')).status_code==401
    assert client.get('/api/products', auth=('admin','my-long-safe-password')).status_code==200

def test_online_sqlite_backup_includes_wal(tmp_path):
    source=tmp_path/'original.sqlite3'; dest=tmp_path/'backup.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE observations(v INTEGER)')
        db.execute('INSERT INTO observations VALUES(1)')
        db.commit()
        db.execute('INSERT INTO observations VALUES(2)')
        db.commit()
        backup_database(source,dest)
    with sqlite3.connect(dest) as restored:
        assert [x[0] for x in restored.execute('SELECT v FROM observations')] == [1,2]
        assert restored.execute('PRAGMA integrity_check').fetchone()[0]=='ok'

def test_time_weighted_prices_are_not_sample_averages():
    from app.analytics import _series
    class Point:
        def __init__(self,v,t):
            self.regular_cents=v
            self.checked_at=t
    t=utc_now()
    rows=[Point(10000,t),Point(10000,t+timedelta(hours=24)),Point(5000,t+timedelta(hours=25))]
    s=_series(rows,'regular_cents',target_cents=7500)
    assert s['average_price']==83.33
    assert s['time_weighted_average_price']==100
    assert s['min_price']==50 and s['min_at']
    assert s['max_discount_from_initial']==50

def test_persistent_error_counter(suite,monkeypatch):
    from app.parsers.common import ScrapeError
    client, _ = suite
    async def failure(_): raise ScrapeError('blocked','403')
    monkeypatch.setattr(service,'fetch_quote',failure)
    p=client.post('/api/products',json={'link':'2190590687','target_price':900}).json()
    assert p['failure_count']==1
    p=client.post(f'/api/products/{p["id"]}/check').json()
    assert p['failure_count']==2
