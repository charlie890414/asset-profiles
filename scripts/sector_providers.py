"""Strict parsers for secondary sector evidence, with explicit crosswalks.

These websites' sector labels are not represented as issuer-native GICS.
"""
import json
import re
from datetime import datetime

from bs4 import BeautifulSoup
from common import sector

STOCKANALYSIS_MARKETS = {'TWSE': ('tpe', 'Taiwan Stock Exchange'),
                         'TPEx': ('tpex', 'Taipei Exchange')}
# Deliberately narrow: other industry groups remain candidates for review.
TRADINGVIEW_CROSSWALK = {
    ('Non-Energy Minerals', 'Other Metals/Minerals'): 'Materials',
    ('Non-Energy Minerals', 'Precious Metals'): 'Materials',
    ('Non-Energy Minerals', 'Steel'): 'Materials',
    ('Non-Energy Minerals', 'Aluminum'): 'Materials',
    ('Non-Energy Minerals', 'Construction Materials'): 'Materials',
    ('Electronic Technology', 'Semiconductors'): 'Information Technology',
    ('Electronic Technology', 'Electronic Components'): 'Information Technology',
    ('Health Technology', 'Pharmaceuticals: Major'): 'Health Care',
    ('Health Technology', 'Biotechnology'): 'Health Care',
}


def provider_urls(symbol, market):
    if not re.fullmatch(r'\d{4}', symbol):
        raise ValueError('Unsupported Taiwan equity symbol')
    sa_market, _ = STOCKANALYSIS_MARKETS[market]
    tv_market = 'TWSE' if market == 'TWSE' else 'TPEX'
    return [('Stock Analysis', f'https://stockanalysis.com/quote/{sa_market}/{symbol}/company/', parse_stockanalysis),
            ('TradingView', f'https://www.tradingview.com/symbols/{tv_market}-{symbol}/financials-overview/', parse_tradingview)]


def _canonical(soup, expected):
    link = soup.find('link', rel='canonical')
    if not link or link.get('href', '').rstrip('/') != expected.rstrip('/'):
        raise ValueError('Canonical market/symbol mismatch')


def parse_stockanalysis(content, symbol, market, url):
    soup = BeautifulSoup(content, 'html.parser')
    _canonical(soup, url)
    fields = {}
    for row in soup.select('tr'):
        cells = row.find_all(['td', 'th'], recursive=False)
        if len(cells) == 2:
            label, value = [c.get_text(' ', strip=True) for c in cells]
            if label in fields and fields[label] != value:
                raise ValueError('Conflicting profile fields')
            fields[label] = value
    if (fields.get('Ticker Symbol') != symbol
            or fields.get('Exchange') != STOCKANALYSIS_MARKETS[market][1]
            or fields.get('Country') != 'Taiwan'):
        raise ValueError('Stock Analysis identity mismatch')
    text = soup.get_text(' ', strip=True)
    dated = re.search(r'Last updated:\s*([A-Za-z]+ \d{1,2}, \d{4})', text)
    as_of = datetime.strptime(dated[1], '%b %d, %Y').date().isoformat() if dated else None
    raw_sector = fields.get('Sector')
    if not raw_sector:
        raise ValueError('Stock Analysis sector missing')
    normalized = sector({'Technology': 'Information Technology', 'Basic Materials': 'Materials',
                         'Consumer Cyclical': 'Consumer Discretionary',
                         'Consumer Defensive': 'Consumer Staples',
                         'Financial Services': 'Financials', 'Healthcare': 'Health Care'}.get(raw_sector, raw_sector))
    return {'sector': normalized, 'raw_sector': raw_sector, 'industry': fields.get('Industry'),
            'isin': fields.get('ISIN Number'), 'as_of_date': as_of,
            'taxonomy': 'Stock Analysis sector crosswalk; not verified native GICS',
            'date_basis': 'Company profile last updated; sector effective date not separately published'}


def parse_tradingview(content, symbol, market, url):
    soup = BeautifulSoup(content, 'html.parser')
    _canonical(soup, url)
    tv_market = 'TWSE' if market == 'TWSE' else 'TPEX'
    symbols = []
    for script in soup.find_all('script', type='application/prs.init-data+json'):
        payload = json.loads(script.get_text())
        if not isinstance(payload, dict):
            raise ValueError('TradingView symbol schema changed')
        for node in payload.values():
            if isinstance(node, dict) and isinstance(node.get('data'), dict):
                value = node['data'].get('symbol')
                if value:
                    if not isinstance(value, dict):
                        raise ValueError('TradingView symbol schema changed')
                    symbols.append(value)
    if len(symbols) != 1 or (symbols[0].get('pro_symbol') != f'{tv_market}:{symbol}'
                            or symbols[0].get('country', '').lower() != 'tw'
                            or symbols[0].get('type') != 'stock'):
        raise ValueError('TradingView identity mismatch')
    labels = {'sector': set(), 'industry': set()}
    isin = None
    for script in soup.find_all('script', type='application/ld+json'):
        doc = json.loads(script.get_text())
        if not isinstance(doc, dict):
            continue
        if doc.get('@type') == 'FinancialProduct':
            if doc.get('tickerSymbol') != symbol:
                raise ValueError('TradingView product mismatch')
            identifiers = doc.get('identifier', [])
            if not isinstance(identifiers, list) or any(not isinstance(i, dict) for i in identifiers):
                raise ValueError('TradingView identifier schema changed')
            isin = next((i['value'] for i in identifiers if i.get('propertyID') == 'ISIN'), None)
        if doc.get('@type') == 'BreadcrumbList':
            items = doc.get('itemListElement', [])
            if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
                raise ValueError('TradingView breadcrumb schema changed')
            for item in items:
                item = item.get('item', {})
                if not isinstance(item, dict):
                    raise ValueError('TradingView breadcrumb item schema changed')
                for label in labels:
                    if f'/sectorandindustry-{label}/' in item.get('@id', ''):
                        labels[label].add(item.get('name'))
    if any(len(values) != 1 or None in values for values in labels.values()):
        raise ValueError('TradingView sector/industry missing or conflicting')
    raw_sector, industry = next(iter(labels['sector'])), next(iter(labels['industry']))
    return {'sector': TRADINGVIEW_CROSSWALK.get((raw_sector, industry)),
            'raw_sector': raw_sector, 'industry': industry, 'isin': isin, 'as_of_date': None,
            'taxonomy': 'TradingView sector/industry crosswalk; not GICS',
            'date_basis': 'Classification date not published; retrieval date only'}
