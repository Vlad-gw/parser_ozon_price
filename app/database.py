"""SQLite models and idempotent migrations for existing installations."""
from datetime import datetime, timezone
from sqlalchemy import (create_engine, Integer, String, Boolean, DateTime,
                        ForeignKey, Index, UniqueConstraint, event, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from .config import DB_PATH

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f'sqlite:///{DB_PATH}', connect_args={'check_same_thread': False, 'timeout': 20}, pool_pre_ping=True)

@event.listens_for(engine, 'connect')
def _sqlite_pragmas(connection, _record):
    c = connection.cursor()
    c.execute('PRAGMA foreign_keys = ON')
    c.execute('PRAGMA journal_mode = WAL')
    c.execute('PRAGMA busy_timeout = 20000')
    c.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

class Product(Base):
    __tablename__ = 'products'
    __table_args__ = (UniqueConstraint('marketplace','sku','price_kind',name='uq_product_marketplace_sku_kind'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(String(24), nullable=False)
    url: Mapped[str] = mapped_column(String(600), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    price_kind: Mapped[str] = mapped_column(String(12), default='regular')
    target_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    current_cents: Mapped[int | None] = mapped_column(Integer)
    current_regular_cents: Mapped[int | None] = mapped_column(Integer)
    current_card_cents: Mapped[int | None] = mapped_column(Integer)
    # An unlabeled schema.org offer is not proof of bank-price classification.
    price_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    image_url: Mapped[str | None] = mapped_column(String(800))
    available: Mapped[bool | None] = mapped_column(Boolean)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_source: Mapped[str | None] = mapped_column(String(100))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(512))
    notified_below: Mapped[bool] = mapped_column(Boolean, default=False)
    alert_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_notify_error: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    points: Mapped[list['PricePoint']] = relationship(back_populates='product', cascade='all, delete-orphan')
    alert_events: Mapped[list['AlertEvent']] = relationship(back_populates='product', cascade='all, delete-orphan')

class PricePoint(Base):
    __tablename__ = 'price_points'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id', ondelete='CASCADE'), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    regular_cents: Mapped[int | None] = mapped_column(Integer)
    card_cents: Mapped[int | None] = mapped_column(Integer)
    price_source: Mapped[str | None] = mapped_column(String(100))
    price_verified: Mapped[bool | None] = mapped_column(Boolean)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    product: Mapped[Product] = relationship(back_populates='points')

class AlertEvent(Base):
    __tablename__ = 'alert_events'
    __table_args__ = (UniqueConstraint('product_id','episode',name='uq_alert_product_episode'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id', ondelete='CASCADE'), nullable=False)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    target_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='pending')
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(512))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    product: Mapped[Product] = relationship(back_populates='alert_events')

Index('ix_points_product_checked', PricePoint.product_id, PricePoint.checked_at)
Index('ix_alert_status', AlertEvent.status, AlertEvent.next_attempt_at)

_PRODUCT_COLUMNS = {
    'current_regular_cents': 'INTEGER', 'current_card_cents': 'INTEGER',
    'price_verified': 'BOOLEAN NOT NULL DEFAULT 0',
    'image_url': 'VARCHAR(800)', 'paused': 'BOOLEAN NOT NULL DEFAULT 0',
    'failure_count': 'INTEGER NOT NULL DEFAULT 0', 'last_source': 'VARCHAR(100)',
    'alert_sequence': 'INTEGER NOT NULL DEFAULT 0',
}
_POINT_COLUMNS = {'regular_cents':'INTEGER', 'card_cents':'INTEGER',
                  'price_source':'VARCHAR(100)', 'price_verified':'BOOLEAN'}

def init_db():
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text('PRAGMA table_info(products)'))}
        for name, definition in _PRODUCT_COLUMNS.items():
            if name not in columns:
                conn.execute(text(f'ALTER TABLE products ADD COLUMN {name} {definition}'))
        # Legacy selected price series is known; the other series is not.
        conn.execute(text("UPDATE products SET current_regular_cents=current_cents WHERE price_kind='regular' AND current_regular_cents IS NULL AND current_cents IS NOT NULL"))
        conn.execute(text("UPDATE products SET current_card_cents=current_cents WHERE price_kind='card' AND current_card_cents IS NULL AND current_cents IS NOT NULL"))
        columns = {row[1] for row in conn.execute(text('PRAGMA table_info(price_points)'))}
        for name, definition in _POINT_COLUMNS.items():
            if name not in columns:
                conn.execute(text(f'ALTER TABLE price_points ADD COLUMN {name} {definition}'))
        conn.execute(text("""UPDATE price_points SET regular_cents=price_cents
            WHERE regular_cents IS NULL AND product_id IN (SELECT id FROM products WHERE price_kind='regular')"""))
        conn.execute(text("""UPDATE price_points SET card_cents=price_cents
            WHERE card_cents IS NULL AND product_id IN (SELECT id FROM products WHERE price_kind='card')"""))
        # Database-level guard against concurrent duplicate additions.
        # If a legacy database already contains duplicates, leave its data intact
        # and warn: the user can manually resolve duplicates before retrying.
        try:
            conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_products_unique_tracking ON products(marketplace, sku, price_kind)'))
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('Cannot create unique product index: %s', type(exc).__name__)
