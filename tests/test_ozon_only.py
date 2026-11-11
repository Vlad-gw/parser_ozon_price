import json
import pytest
from app.parsers.common import parse_ref, rub_text_to_cents, money_to_cents
from app.parsers.ozon import (
    parse_ozon_widget_payload, parse_json_ld,
    parse_visible_price_widget, _is_matching_open_tab,
)


def test_ozon_id_and_url():
    assert parse_ref('2190590687').url == 'https://www.ozon.ru/product/2190590687/'
    ref = parse_ref('https://www.ozon.ru/product/a-test-2190590687/?utm_source=test')
    assert ref.sku == '2190590687'
    assert ref.url == 'https://www.ozon.ru/product/a-test-2190590687/'

@pytest.mark.parametrize('invalid', [
    'https://wildberries.ru/catalog/13789135/detail.aspx',
    'https://www.ozon.ru.evil.test/product/2190590687/',
    'http://www.ozon.ru/product/2190590687/',
    'https://127.0.0.1/product/2190590687/',
    'https://www.ozon.ru/category/2190590687/',
])
def test_ozon_rejects_nonproduct(invalid):
    with pytest.raises(ValueError):
        parse_ref(invalid)


def test_money():
    assert rub_text_to_cents('12\u202f340,50 ₽') == 1234050
    assert rub_text_to_cents(884) == 88400
    assert rub_text_to_cents('нет цены') is None
    assert money_to_cents('500.50') == 50050


def test_ozon_widget_price():
    obj = {'widgetStates': {
        'webProductHeading-1': json.dumps({'title': 'Чехол для мотоцикла'}),
        'webPrice-1': json.dumps({'price': '884 ₽', 'cardPrice': '799 ₽',
                                  'link': '/cart?product_id=2190590687'}),
    }}
    q = parse_ozon_widget_payload(obj, '2190590687')
    assert q.name == 'Чехол для мотоцикла'
    assert q.regular_cents == 88400 and q.card_cents == 79900
    assert parse_ozon_widget_payload(obj, '123456789') is None


def test_ozon_jsonld_and_cross_sku():
    raw = json.dumps({'@type': 'Product', 'sku': '2190590687',
                      'name': 'Чехол', 'offers': {'price': '884.00', 'priceCurrency': 'RUB'}})
    quote = parse_json_ld([raw], '2190590687')
    assert quote.name == 'Чехол' and quote.regular_cents == 88400
    assert parse_json_ld([raw], '777777777') is None


def test_existing_tab_exact_match():
    assert _is_matching_open_tab('https://www.ozon.ru/product/2190590687/?x=1', '2190590687')
    assert _is_matching_open_tab('https://www.ozon.ru/product/some-thing-2190590687/', '2190590687')
    assert not _is_matching_open_tab('https://www.ozon.ru/product/12190590687/', '2190590687')
    assert not _is_matching_open_tab('https://www.ozon.ru/product/2190590687.evil/', '2190590687')


def test_visible_widget_with_label():
    q = parse_visible_price_widget('799 ₽ с Ozon Картой\n884 ₽ обычная цена', 'Чехол')
    assert q.card_cents == 79900 and q.regular_cents == 88400
