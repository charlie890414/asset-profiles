from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

import jsonschema
import pycountry

ROOT = Path(__file__).resolve().parents[1]
SECTORS = ['Energy', 'Materials', 'Industrials', 'Consumer Discretionary',
           'Consumer Staples', 'Health Care', 'Financials', 'Information Technology',
           'Communication Services', 'Utilities', 'Real Estate']
SECTOR_ALIASES = {
    '能源': 'Energy', '原材料': 'Materials', '原物料': 'Materials', '工業': 'Industrials',
    '非必需消費品': 'Consumer Discretionary', '必需消費品': 'Consumer Staples',
    '醫療保健': 'Health Care', '金融': 'Financials', '資訊科技': 'Information Technology',
    '通訊': 'Communication Services', '公用事業': 'Utilities', '房地產': 'Real Estate',
    'Communication': 'Communication Services', '通訊服務': 'Communication Services',
}
COUNTRY_ALIASES = dict(zip(
    ['美國','日本','台灣','臺灣','英國','加拿大','韓國','中國','法國','德國','瑞士','澳洲','印度','荷蘭','義大利',
     '瑞典','香港','新加坡','丹麥','愛爾蘭','南非','芬蘭','比利時','以色列','墨西哥','挪威','波蘭','奧地利','希臘','葡萄牙','秘魯','紐西蘭','西班牙'],
    ['US','JP','TW','TW','GB','CA','KR','CN','FR','DE','CH','AU','IN','NL','IT','SE','HK','SG','DK','IE','ZA','FI','BE','IL','MX','NO','PL','AT','GR','PT','PE','NZ','ES']))
COUNTRY_ALIASES.update({'South Korea':'KR', 'Korea (South)':'KR', 'Taiwan':'TW', 'United Kingdom':'GB', 'Russia':'RU'})


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def number(value):
    result = Decimal(str(value).replace(',', '').replace('%', '').strip())
    if not result.is_finite():
        raise ValueError('Non-finite weight/value')
    return result


def country(value):
    value = str(value).strip()
    code = COUNTRY_ALIASES.get(value, value.upper())
    match = pycountry.countries.get(alpha_2=code)
    if not match:
        try:
            match = pycountry.countries.lookup(value)
        except LookupError:
            return None
    return match.alpha_2


def sector(value):
    value = SECTOR_ALIASES.get(value, value)
    return value if value in SECTORS else None


def safe_key(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,60}', value):
        raise ValueError(f'Unsafe identifier: {value!r}')
    if value.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(10)],*[f'LPT{i}' for i in range(10)]}:
        raise ValueError('Windows reserved filename')
    return value


def write_json(path, value):
    path = Path(path)
    body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n'
    if path.exists() and path.read_text(encoding='utf-8') == body:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(body, encoding='utf-8')
    temp.replace(path)
    return True


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def basis_points(weights, normalize=False):
    """Never scale partial coverage up unless the denominator is explicitly complete."""
    weights = {k: number(v) for k, v in weights.items()}
    if any(v < 0 for v in weights.values()):
        raise ValueError('Negative allocation requires derivative review')
    total = sum(weights.values(), Decimal(0))
    if total > Decimal('1.005') or total <= 0:
        raise ValueError(f'Invalid allocation total: {total}')
    if not normalize and total > 1:
        raise ValueError('Partial allocation exceeds 100%')
    raw = {k: v / total * 10000 if normalize else v * 10000 for k, v in weights.items()}
    target = 10000 if normalize else min(10000, int(total * 10000))
    result = {k: int(v.to_integral_value(rounding=ROUND_FLOOR)) for k, v in raw.items()}
    for key in sorted(raw, key=lambda k: (-(raw[k] - result[k]), k))[:target - sum(result.values())]:
        result[key] += 1
    return result


def validate_profile(profile):
    schema = json.loads((ROOT / 'schema/etf.schema.json').read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(profile)
    for field in ['sector_weights','country_weights','asset_class_weights']:
        rows = profile.get(field)
        if rows is not None and (not rows or abs(sum(number(r['weight']) for r in rows)-1) > Decimal('.005')):
            raise ValueError(f'{field}: incomplete distribution')
    for row in profile.get('sector_weights', []):
        if row['sector'] not in SECTORS and row['sector'] != 'Other':
            raise ValueError('Unverified GICS sector')
    for row in profile.get('country_weights', []):
        code = row.get('country_code')
        if code is None and row.get('country') == 'Other':
            continue
        if not country(code):
            raise ValueError('Unknown country code')
    if sum(number(r['weight']) for r in profile.get('top_holdings', [])) > Decimal('1.005'):
        raise ValueError('Holdings exceed NAV; derivative review required')
