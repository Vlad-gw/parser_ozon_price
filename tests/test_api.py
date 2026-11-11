"""API smoke tests using a fake price source; no network or Chrome required."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main, service
from app.database import Base
from app.parsers.common import Quote


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "test.sqlite3"}')
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, 'SessionLocal', session_factory)
    monkeypatch.setattr(service, 'SessionLocal', session_factory)
    monkeypatch.setattr(main.config, 'SCHEDULER_ENABLED', False)

    async def fake_quote(_ref):
        return Quote(name='Тестовый товар Ozon', regular_cents=88400,
                     card_cents=79900, available=True, source='test:mock')

    monkeypatch.setattr(service, 'fetch_quote', fake_quote)
    with TestClient(main.app) as test_client:
        yield test_client
    engine.dispose()


def test_ozon_end_to_end(client):
    assert client.get('/').status_code == 200
    assert client.get('/api/health').json()['ok'] is True
    assert client.get('/api/products').json() == []
    r = client.post('/api/products', json={
        'link': 'https://www.ozon.ru/product/2190590687/',
        'target_price': 500, 'price_kind': 'regular',
    })
    assert r.status_code == 201, r.text
    p = r.json()
    assert p['marketplace'] == 'ozon' and p['current_price'] == 884.0
    assert p['last_error'] is None
    assert len(client.get(f'/api/products/{p["id"]}/history').json()) == 1
    assert client.post(f'/api/products/{p["id"]}/check').status_code == 200
    assert len(client.get(f'/api/products/{p["id"]}/history').json()) == 2
    assert client.patch(f'/api/products/{p["id"]}', json={'target_price': 400}).status_code == 200
    assert client.delete(f'/api/products/{p["id"]}').json() == {'ok': True}
    assert client.get('/api/products').json() == []


def test_rejects_wildberries(client):
    r = client.post('/api/products', json={
        'link': 'https://www.wildberries.ru/catalog/13789135/detail.aspx',
        'target_price': 500,
    })
    assert r.status_code == 422, r.text
