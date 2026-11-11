"""One-off Ozon price check: python -m app.cli URL_OR_SKU."""
import argparse
import asyncio
import json

from .parsers import resolve_ref, ScrapeError
from .service import fetch_quote


async def run():
    parser = argparse.ArgumentParser(description='Проверить актуальную цену Ozon')
    parser.add_argument('link', help='Полная/короткая ссылка Ozon или числовой артикул')
    args = parser.parse_args()
    ref = await resolve_ref(args.link, 'ozon')
    q = await fetch_quote(ref)
    print(json.dumps({
        'marketplace': 'ozon', 'sku': ref.sku, 'name': q.name,
        'price_regular_rub': q.regular_cents / 100 if q.regular_cents is not None else None,
        'price_ozon_card_rub': q.card_cents / 100 if q.card_cents is not None else None,
        'available': q.available, 'source': q.source, 'url': ref.url,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except (ValueError, ScrapeError) as exc:
        print(f'Ошибка: {exc}')
        raise SystemExit(1)
