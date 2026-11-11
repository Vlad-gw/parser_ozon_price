"""Ozon-only REST API. Keep exactly one FastAPI worker / Telegram poller."""
from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from . import config
from .database import Product, PricePoint, AlertEvent, SessionLocal, init_db, utc_now
from .analytics import analytics_for_product
from .parsers import money_to_cents, resolve_ref
from .service import check_all, check_product, product_dict, _cancel_unsent, _browser_lock
from .maintenance import scheduled_backup
from .telegram_bot import configured as telegram_configured, poll_updates, stop_pending

scheduler = AsyncIOScheduler(timezone='UTC')

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    telegram_task = None
    if config.SCHEDULER_ENABLED:
        scheduler.add_job(check_all, 'interval', minutes=config.CHECK_INTERVAL_MIN,
                          id='check_prices', max_instances=1, coalesce=True,
                          misfire_grace_time=180, replace_existing=True)
        scheduler.add_job(scheduled_backup, 'interval', hours=config.BACKUP_INTERVAL_HOURS,
                          id='backup_sqlite', max_instances=1, coalesce=True, replace_existing=True)
        scheduler.start()
    if telegram_configured() and config.TELEGRAM_POLLING_ENABLED:
        telegram_task = asyncio.create_task(poll_updates(), name='telegram-long-poll')
    try:
        yield
    finally:
        if telegram_task:
            telegram_task.cancel()
            await asyncio.gather(telegram_task, return_exceptions=True)
        await stop_pending()
        if scheduler.running:
            scheduler.shutdown(wait=False)

app = FastAPI(title='Ozon Price Tracker', version='2.0.0', lifespan=lifespan)
STATIC = Path(__file__).parent / 'static'

@app.middleware('http')
async def protect_site(request: Request, call_next):
    """HTTP Basic for production; without a password allow only direct localhost use.

    Always enable APP_PASSWORD behind any public reverse proxy and HTTPS.
    """
    if config.APP_PASSWORD:
        header = request.headers.get('authorization', '')
        allowed = False
        if header.lower().startswith('basic '):
            try:
                username, password = base64.b64decode(header.split(' ', 1)[1], validate=True).decode('utf-8').split(':', 1)
                allowed = secrets.compare_digest(username.encode(), b'admin') and secrets.compare_digest(password.encode(), config.APP_PASSWORD.encode())
            except (ValueError, UnicodeDecodeError, binascii.Error):
                pass
        if not allowed:
            return Response(status_code=401, headers={'WWW-Authenticate':'Basic realm="Ozon Price Tracker"',
                                                        'Cache-Control':'no-store'})
    elif request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient'):
        # The supplied macOS start script binds to loopback. Never expose an
        # unprotected API to arbitrary remote clients even if uvicorn is rebound.
        return JSONResponse({'detail': 'Remote access requires APP_PASSWORD and HTTPS'}, status_code=403)
    origin = request.headers.get('origin')
    if request.method in ('POST','PUT','PATCH','DELETE') and origin:
        from urllib.parse import urlsplit
        # A cross-origin page must not be able to mutate a Basic-authenticated API.
        if urlsplit(origin).netloc.lower() != request.headers.get('host', '').lower():
            return JSONResponse({'detail': 'Cross-origin write denied'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

app.mount('/static', StaticFiles(directory=STATIC), name='static')

class ProductCreate(BaseModel):
    link: str = Field(min_length=5, max_length=1000)
    target_price: Decimal = Field(gt=0)
    price_kind: Literal['regular', 'card'] = 'regular'

class ProductUpdate(BaseModel):
    target_price: Decimal = Field(gt=0)

class ProductPause(BaseModel):
    paused: bool

@app.get('/')
async def home():
    return FileResponse(STATIC / 'index.html')

@app.get('/favicon.ico')
async def favicon():
    return FileResponse(STATIC / 'favicon.svg', media_type='image/svg+xml')

@app.get('/api/health')
async def health():
    job = (scheduler.get_job('check_prices') if scheduler.running and hasattr(scheduler, 'get_job') else None)
    with SessionLocal() as db:
        total = db.scalar(select(func.count(Product.id)).where(Product.marketplace == 'ozon')) or 0
        active = db.scalar(select(func.count(Product.id)).where(Product.marketplace == 'ozon', Product.paused.is_(False))) or 0
        failures = db.scalar(select(func.count(Product.id)).where(Product.last_error.is_not(None), Product.paused.is_(False))) or 0
        pending = db.scalar(select(func.count(AlertEvent.id)).where(AlertEvent.status.in_(['pending', 'sending']))) or 0
    return {'ok': True, 'scheduler_running': scheduler.running,
            'telegram_configured': telegram_configured(),
            'telegram_polling_enabled': config.TELEGRAM_POLLING_ENABLED and telegram_configured(),
            'telegram_reports_enabled': False,
            'interval_minutes': config.CHECK_INTERVAL_MIN,
            'next_check_at': job.next_run_time.isoformat() if job and job.next_run_time else None,
            'total_products': total, 'active_products': active,
            'products_with_errors': failures, 'pending_alerts': pending}

@app.get('/api/products')
async def list_products():
    with SessionLocal() as db:
        data = list(db.scalars(select(Product).where(Product.marketplace == 'ozon').order_by(Product.id.desc())))
        return [product_dict(p) for p in data]

@app.post('/api/products', status_code=201)
async def add_product(body: ProductCreate):
    try:
        async with _browser_lock:
            ref = await resolve_ref(body.link, 'ozon')
        cents = money_to_cents(body.target_price)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with SessionLocal() as db:
        exists = db.scalar(select(Product).where(
            Product.marketplace == ref.marketplace, Product.sku == ref.sku,
            Product.price_kind == body.price_kind))
        if exists:
            raise HTTPException(status_code=409, detail=f'Этот товар уже отслеживается (ID {exists.id})')
        p = Product(marketplace=ref.marketplace, sku=ref.sku, url=ref.url,
                    name=f'Ozon #{ref.sku}', price_kind=body.price_kind,
                    target_cents=cents)
        db.add(p)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail='Этот товар уже добавлен другим запросом') from None
        product_id = p.id
    return await check_product(product_id)

@app.post('/api/products/{product_id}/check')
async def check_now(product_id: int):
    result = await check_product(product_id, force=True)
    if result is None:
        raise HTTPException(status_code=404, detail='Товар не найден')
    return result

@app.patch('/api/products/{product_id}')
async def change_target(product_id: int, body: ProductUpdate):
    try:
        cents = money_to_cents(body.target_price)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None or p.marketplace != 'ozon':
            raise HTTPException(status_code=404, detail='Товар не найден')
        if p.target_cents != cents:
            p.target_cents = cents
            p.notified_below = False
            p.last_notify_error = None
            _cancel_unsent(db, p.id)
        db.commit()
        return product_dict(p)

@app.patch('/api/products/{product_id}/pause')
async def set_pause(product_id: int, body: ProductPause):
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None or p.marketplace != 'ozon':
            raise HTTPException(status_code=404, detail='Товар не найден')
        p.paused = body.paused
        if p.paused:
            _cancel_unsent(db, p.id)
            p.notified_below = False
        db.commit()
        return product_dict(p)

@app.delete('/api/products/{product_id}')
async def delete_product(product_id: int):
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None or p.marketplace != 'ozon':
            raise HTTPException(status_code=404, detail='Товар не найден')
        db.delete(p)
        db.commit()
    return {'ok': True}

@app.get('/api/products/{product_id}/history')
async def history(product_id: int, limit: int = 300):
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None or p.marketplace != 'ozon':
            raise HTTPException(status_code=404, detail='Товар не найден')
        n = max(1, min(limit, 3000))
        points = list(db.scalars(select(PricePoint).where(
            PricePoint.product_id == product_id).order_by(PricePoint.checked_at.desc(), PricePoint.id.desc()).limit(n)))
        return [{'price': x.price_cents / 100,
                 'regular_price': x.regular_cents / 100 if x.regular_cents is not None else None,
                 'card_price': x.card_cents / 100 if x.card_cents is not None else None,
                 'price_verified': x.price_verified, 'price_source': x.price_source,
                 'checked_at': x.checked_at.isoformat()} for x in reversed(points)]

@app.get('/api/products/{product_id}/analytics')
async def analytics(product_id: int):
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None or p.marketplace != 'ozon':
            raise HTTPException(status_code=404, detail='Товар не найден')
        return analytics_for_product(db, p)
