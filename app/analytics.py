"""Price analytics over actual successful observations, keeping both bank price series."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import select
from sqlalchemy.orm import Session
from .database import PricePoint, Product


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _money(cents) -> float:
    return float((Decimal(cents) / 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _pct(delta: int, base: int) -> float | None:
    return (float((Decimal(delta) * 100 / Decimal(base)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))
            if base > 0 else None)


def _series(rows: list[PricePoint], attr: str, *, target_cents: int | None = None) -> dict | None:
    quotes = [(getattr(p, attr), _as_utc(p.checked_at)) for p in rows if getattr(p, attr) is not None]
    if not quotes:
        return None
    initial, first_time = quotes[0]
    latest, last_time = quotes[-1]
    prices = [x[0] for x in quotes]
    cheapest = min(prices)
    expensive = max(prices)
    min_at = next(t for v, t in quotes if v == cheapest)
    max_at = next(t for v, t in quotes if v == expensive)
    prev = quotes[-2][0] if len(quotes) > 1 else None

    def change_window(days):
        cutoff = last_time - timedelta(days=days)
        candidates = [(v, t) for v, t in quotes if t <= cutoff]
        if not candidates:
            return None
        ref, ref_at = candidates[-1]
        return {'amount': _money(latest-ref), 'percent':_pct(latest-ref,ref), 'from_at':ref_at.isoformat()}

    weighted_sum = Decimal(0)
    observed_seconds = Decimal(0)
    below_seconds = Decimal(0)
    for (value, at), (_, after) in zip(quotes, quotes[1:]):
        # Assume the last observed price holds until the next observation.
        # Gaps are not measured: estimates are NOT actual price histories.
        seconds = Decimal(str(max(0, (after-at).total_seconds())))
        weighted_sum += Decimal(value) * seconds
        observed_seconds += seconds
        if target_cents is not None and value <= target_cents:
            below_seconds += seconds

    return {
        'initial_price': _money(initial), 'initial_at': first_time.isoformat(),
        'latest_price': _money(latest), 'latest_at': last_time.isoformat(),
        'change_amount': _money(latest-initial), 'change_percent': _pct(latest-initial, initial),
        'min_price': _money(cheapest), 'max_price': _money(expensive),
        'min_at': min_at.isoformat(), 'max_at': max_at.isoformat(),
        'max_discount_from_initial': _money(max(0, initial-cheapest)),
        'max_discount_percent': _pct(cheapest-initial, initial) if cheapest < initial else 0.0,
        'average_price': _money(Decimal(sum(prices))/len(prices)),
        'time_weighted_average_price': (_money(weighted_sum/observed_seconds) if observed_seconds > 0 else None),
        'below_target_estimated_hours': (round(float(below_seconds/Decimal(3600)), 1) if target_cents is not None and observed_seconds else None),
        'observed_span_hours': round(float(observed_seconds/Decimal(3600)), 1),
        'observations': len(quotes),
        'previous_change_amount': _money(latest-prev) if prev is not None else None,
        'previous_change_percent': _pct(latest-prev,prev) if prev is not None else None,
        'change_24h': change_window(1), 'change_7d': change_window(7),
    }


def analytics_for_product(db: Session, product: Product) -> dict:
    rows = list(db.scalars(select(PricePoint).where(PricePoint.product_id == product.id)
                           .order_by(PricePoint.checked_at, PricePoint.id)))
    regular = _series(rows, 'regular_cents', target_cents=product.target_cents if product.price_kind == 'regular' else None)
    card = _series(rows, 'card_cents', target_cents=product.target_cents if product.price_kind == 'card' else None)
    return {'selected_kind': product.price_kind,
            'selected': card if product.price_kind == 'card' else regular,
            'card': card, 'regular': regular,
            'created_at': product.created_at.isoformat() if product.created_at else None,
            'sampled_only': True}
