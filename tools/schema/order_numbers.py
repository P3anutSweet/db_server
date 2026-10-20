"""Country-scoped, durable order-number allocation. No network or credentials."""
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sqlite3
from collections import defaultdict
from zoneinfo import ZoneInfo

COUNTRIES = {'巴西': ('SA', 'BRA'), '阿根廷': ('SA', 'ARG'), '越南': ('SEA', 'VIE'),
             '菲律宾': ('SEA', 'PHI'), '香港': ('SEA', 'HKG'),
             '波兰': ('EU', 'POL'), '土耳其': ('EU', 'TUR')}
CHANNEL_STATES = ['预测', '已下单', '客户付款中', '提货中', '已提货', '已交付']
ADMIN_STATES = ['需确认订单状态', '需确认付款状态', '需确认提货状态', '需确认交付状态', '已完结']
ORDERED_STATES = set(CHANNEL_STATES) - {'预测'}


def business_day(value):
    if not value:
        return None
    if len(value) == 10:
        return dt.date.fromisoformat(value)
    date = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return date.replace(tzinfo=date.tzinfo or ZoneInfo('Asia/Shanghai')).astimezone(ZoneInfo('Asia/Shanghai')).date()


def order_number(region, country, day, sequence):
    if not isinstance(day, dt.date) or not 1 <= sequence <= 999999:
        raise ValueError('Real order date and six-digit country sequence required.')
    if (region, country) not in COUNTRIES.values():
        raise ValueError('Unknown region/country code combination.')
    return f'{region}{day:%Y%m%d}{country}{sequence:06d}'


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class CountryCounter:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS counters(country TEXT PRIMARY KEY, value INTEGER NOT NULL);'
                'CREATE TABLE IF NOT EXISTS reservations(record TEXT PRIMARY KEY, country TEXT NOT NULL, seq INTEGER NOT NULL, UNIQUE(country,seq));')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=ClosingConnection)

    def floor(self, country, value):
        with self.connect() as db:
            db.execute('INSERT INTO counters VALUES(?,?) ON CONFLICT(country) DO UPDATE SET value=MAX(value,excluded.value)', (country, value))

    def reserve(self, country, record, existing=None):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT country,seq FROM reservations WHERE record=?', (record,)).fetchone()
            if old:
                if old[0] != country or (existing is not None and old[1] != existing):
                    raise ValueError('Existing order country/sequence changed; manual review required.')
                db.commit()
                return old[1]
            n = db.execute('SELECT value FROM counters WHERE country=?', (country,)).fetchone()
            seq = int(existing) if existing is not None else (n[0] if n else 0) + 1
            if not 1 <= seq <= 999999:
                raise ValueError('Six-digit country sequence exhausted; never wrap or reuse.')
            db.execute('INSERT INTO reservations VALUES(?,?,?)', (record, country, seq))
            db.execute('INSERT INTO counters VALUES(?,?) ON CONFLICT(country) DO UPDATE SET value=MAX(value,excluded.value)', (country, seq))
            db.commit()
            return seq
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def amount(quantity, price):
    if quantity is None or price is None:
        return None
    return float((Decimal(str(quantity)) * Decimal(str(price))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def aggregate_orders(rows):
    """Monthly order cohorts, independent of forecast/delivery/pickup dates.

    Rows use logical keys. Undated orders are counted by market for visible coverage
    warnings; they never receive a fabricated month. Forecast-stage rows are excluded.
    A missing price makes the whole cohort value unknown rather than a partial total.
    """
    groups, gaps = {}, defaultdict(int)
    for r in rows:
        market, sku = r.get('market'), r.get('sku')
        if not market:
            continue
        if r.get('channel_status') == '预测':
            continue
        day = business_day(r.get('order_date'))
        if not day:
            gaps[market] += 1
            continue
        if not sku:
            gaps[market] += 1
            continue
        key = (day.strftime('%Y-%m'), market, sku)
        g = groups.setdefault(key, {'quantity': 0, 'picked': 0, 'pending': 0,
                                   'total': 0, 'missing_price': 0, 'invalid': 0, 'records': []})
        g['records'].append(r['id'])
        q, picked = r.get('order_quantity'), r.get('picked_quantity')
        if r.get('channel_status') not in ORDERED_STATES or q is None or picked is None or q < 0 or picked < 0 or picked > q:
            g['invalid'] += 1
            continue
        g['quantity'] += q
        g['picked'] += picked
        g['pending'] += q - picked
        value = amount(q, r.get('unit_price'))
        if value is None:
            g['missing_price'] += 1
        else:
            g['total'] += value
    for g in groups.values():
        if g['missing_price'] or g['invalid']:
            g['total'] = None
        else:
            g['total'] = float(Decimal(str(g['total'])).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    return groups, dict(gaps)
