"""Issuer adapters. Unknown structures fail closed; no guessed API results."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import ssl
import subprocess
import shutil
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from openpyxl import load_workbook

from common import ROOT, number, country, sector, now
from taiwan_lookup import lookup_missing


class Fetcher:
    def __init__(self, cache=ROOT / '.cache', offline=False):
        self.cache, self.offline = Path(cache), offline
        self.evidence = []

    def get(self, url, body=None, headers=None):
        if not url.startswith('https://'):
            raise ValueError('Sources must use HTTPS')
        data = json.dumps(body).encode() if body is not None else None
        key = hashlib.sha256(url.encode() + (data or b'')).hexdigest()
        path = self.cache / key
        meta_path = self.cache / (key + '.json')
        if self.offline:
            content = path.read_bytes()
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        else:
            # Keep hostname and CA-chain verification. The issuer's legacy CA lacks
            # a Subject Key Identifier, which Python 3.13 strict mode rejects.
            context = ssl.create_default_context()
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
            request_headers = {'User-Agent': 'Mozilla/5.0 (compatible; ETFProfileResearch/1.0)',
                               'Accept': '*/*'}
            if headers:
                request_headers.update(headers)
            if data is not None:
                request_headers['Content-Type'] = 'application/json'
            if url == 'https://www.vanguard.co.uk/gpx/graphql':
                request_headers['X-Consumer-ID'] = 'uk2'  # Public website client identifier.
            request = urllib.request.Request(url, data=data, headers=request_headers)
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, context=context, timeout=40) as response:
                        content = response.read(25_000_001)
                        if len(content) > 25_000_000:
                            raise ValueError('Source exceeds 25 MB limit')
                    break
                except (OSError, TimeoutError):
                    if attempt == 2:
                        raise
                    time.sleep(1 + attempt)
            self.cache.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            meta = {'source_url': url, 'fetched_at': now(), 'sha256': hashlib.sha256(content).hexdigest()}
            meta_path.write_text(json.dumps(meta), encoding='utf-8')
        if hashlib.sha256(content).hexdigest() != meta['sha256']:
            raise ValueError('Cache hash mismatch')
        self.evidence.append(meta)
        return content


def parse_date(value):
    value = str(value).strip().replace('Sept', 'Sep')
    match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', value)
    if match:
        return datetime(*map(int, match.groups())).date().isoformat()
    for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y%m%d', '%b %d, %Y', '%b %d %Y', '%d/%b/%Y', '%d %b %Y', '%d-%b-%Y', '%m/%d/%Y']:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f'Unknown source date: {value!r}')


def _fund_soup(content):
    """Decode domestic fund pages while tolerating UTF-8 and Big5 responses."""
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = content.decode('cp950', errors='replace')
    return BeautifulSoup(text, 'html.parser')


def _fund_percent(value):
    match = re.search(r'[-+]?\d+(?:\.\d+)?', str(value).replace(',', ''))
    if not match:
        return None
    return float(number(match.group(0)) / 100)


def _fund_date(text):
    match = re.search(r'(?:資料(?:日期|月份)|截至|As of)[：:\s]*((?:20\d{2})[/-]\d{1,2}[/-]\d{1,2})', text, re.I)
    return parse_date(match.group(1)) if match else None


def _fund_table_rows(table):
    rows = []
    for row in table.find_all('tr'):
        # Layout tables wrap the data tables. Read each row only in its
        # owning table, and do not treat nested table text as a data cell.
        if row.find_parent('table') is not table:
            continue
        cells = row.find_all(['th', 'td'], recursive=False)
        if any(cell.find('table') is not None for cell in cells):
            continue
        values = [cell.get_text(' ', strip=True) for cell in cells]
        if values:
            rows.append(values)
    return rows


def moneydj_fund(entry, source, fetch):
    """Read MoneyDJ FundDJ allocation and monthly top-ten tables.

    FundDJ labels the asset allocation table as ``依產業`` even when its rows
    are underlying fund categories (US equity, bond, cash, ...).  We map only
    those explicit buckets to asset classes and retain the original denominator
    in the returned metadata.  GICS and country weights are never guessed from
    a partial table.
    """
    content = fetch.get(source['url'])
    soup = _fund_soup(content)
    page_text = soup.get_text(' ', strip=True)
    marker = source.get('name_contains') or entry.get('name_contains')
    if marker and marker not in page_text:
        raise ValueError('Fund identity mismatch')
    tables = soup.find_all('table')
    asset_rows = []
    top_holdings = []
    dates = []
    page_date = _fund_date(page_text)
    if page_date:
        dates.append(page_date)
    asset_aliases = {
        '美國股票型': 'Equity', '新興亞洲股票型': 'Equity', '全球股票型': 'Equity',
        '歐洲股票型': 'Equity', '日本股票型': 'Equity', '中國': 'Equity',
        '趨勢產業型': 'Equity', '股票型': 'Equity', '債券型': 'Fixed Income',
        '流動資金': 'Cash', '現金': 'Cash', '其他': 'Other',
    }
    for table in tables:
        rows = _fund_table_rows(table)
        if not rows:
            continue
        table_text = ' '.join(' '.join(row) for row in rows)
        found_date = _fund_date(table_text)
        if found_date:
            dates.append(found_date)
        header_index = next((index for index, row in enumerate(rows)
                             if '產業' in ' '.join(row) and '比例' in ' '.join(row)), None)
        holdings_header_index = next((index for index, row in enumerate(rows)
                                      if '投資名稱' in ' '.join(row) and '比例' in ' '.join(row)), None)
        if header_index is not None:
            parsed = []
            for row in rows[header_index + 1:]:
                if len(row) < 2:
                    continue
                label = row[0].strip()
                weight = _fund_percent(row[-1])
                if label in asset_aliases and weight is not None:
                    parsed.append((asset_aliases[label], weight))
            if parsed:
                asset_rows.extend(parsed)
        if holdings_header_index is not None:
            # A FundDJ row contains two repeated groups of name/amount/ratio/change.
            for row in rows[holdings_header_index + 1:]:
                for offset in range(0, len(row), 4):
                    if offset + 2 >= len(row):
                        continue
                    name = row[offset].strip()
                    weight = _fund_percent(row[offset + 2])
                    if not name or weight is None or re.fullmatch(r'[-+]?\d[\d,.]*%?', name):
                        continue
                    top_holdings.append({'name': name, 'weight': weight})
    configured = source.get('asset_class_weights')
    if configured:
        asset_weights = {str(key): float(number(value)) for key, value in configured.items()}
        asset_basis = source.get('asset_class_basis', 'issuer published NAV allocation')
    elif asset_rows:
        asset_weights = {}
        for key, value in asset_rows:
            asset_weights[key] = asset_weights.get(key, 0) + value
        total = sum(asset_weights.values())
        if total <= 0 or total > 1.005:
            raise ValueError('Invalid fund asset allocation total')
        # FundDJ tables generally include an explicit 100% residual.  If the
        # page is a top-category table, preserve the missing amount as Other.
        if total < .995:
            asset_weights['Other'] = asset_weights.get('Other', 0) + (1 - total)
        asset_basis = 'MoneyDJ FundDJ reported fund-category allocation'
    else:
        raise ValueError('Fund asset allocation table missing')
    if not top_holdings and source.get('top_holdings'):
        top_holdings = source['top_holdings']
    as_of = source.get('as_of_date')
    if dates:
        as_of = max(dates)
    if not as_of:
        raise ValueError('Fund holdings date missing')
    result = {'as_of_date': parse_date(as_of), 'name': entry['name'],
              'isin': source.get('isin') or entry.get('isin'),
              'source_name': source.get('source_name', 'MoneyDJ FundDJ'),
              'asset_class_weights': asset_weights,
              'asset_class_basis': asset_basis,
              'top_holdings': top_holdings[:10], 'complete_holdings': False,
              'classification_dates': [parse_date(as_of)],
              'sector_taxonomy': 'Unavailable; FundDJ does not publish complete GICS look-through',
              'country_basis': 'Unavailable; FundDJ table is partial or absent',
              'issues': ['FundDJ reports monthly top holdings; quarterly full holdings are not guaranteed in this endpoint.']}
    return result


def parse_blackrock(content):
    text = content.decode('utf-8-sig')
    lines = text.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith('Ticker,Name,Sector,'))
    date_row = next(csv.reader([line]) for line in lines[:header] if line.startswith(('Fund Holdings as of,','截至,')))
    as_of = parse_date(next(date_row)[1])
    holdings = []
    for row in csv.DictReader(io.StringIO('\n'.join(lines[header:]))):
        if not row.get('Weight (%)') or not row.get('Market Value'):
            continue
        holdings.append({'symbol':row['Ticker'], 'name':row['Name'],
                         'weight':float(number(row['Weight (%)']) / 100),
                         'value':float(number(row['Market Value'])),
                         'country':country(row['Location']), 'sector':sector(row['Sector']),
                         'equity':row['Asset Class'] in ['Equity', '指數股票型'],
                         'source_asset_class':row['Asset Class']})
    if not holdings:
        raise ValueError('Empty BlackRock holdings')
    return {'as_of_date':as_of, 'holdings':holdings, 'complete_holdings':True,
            'sector_taxonomy':'GICS', 'country_basis':'issuer Location',
            'denominator':'equity market value', 'issues':[]}


def parse_blackrock_xml(content, default_country='IN'):
    """Parse BlackRock's SpreadsheetML holdings export used by nested UCITS ETFs."""
    ns = {'ss': 'urn:schemas-microsoft-com:office:spreadsheet'}
    root = ET.fromstring(content)
    worksheet = next((node for node in root.findall('ss:Worksheet', ns)
                      if node.attrib.get('{urn:schemas-microsoft-com:office:spreadsheet}Name') == 'Holdings'), None)
    if worksheet is None:
        raise ValueError('BlackRock XML has no Holdings worksheet')
    rows = []
    for row in worksheet.findall('ss:Table/ss:Row', ns):
        values = []
        for cell in row.findall('ss:Cell', ns):
            data = cell.find('ss:Data', ns)
            values.append('' if data is None or data.text is None else data.text.strip())
        rows.append(values)
    header_index = next((i for i, row in enumerate(rows) if 'Issuer Ticker' in row and 'Weight (%)' in row), None)
    if header_index is None:
        raise ValueError('BlackRock XML holdings header missing')
    header = {name: index for index, name in enumerate(rows[header_index])}
    date_value = None
    for row in rows:
        if len(row) >= 2 and row[0].lower() == 'as of':
            date_value = parse_date(row[1])
            break
    if date_value is None:
        raise ValueError('BlackRock XML holdings date missing')
    holdings = []
    for row in rows[header_index + 1:]:
        if len(row) <= max(header.values()):
            continue
        ticker = row[header['Issuer Ticker']].strip()
        weight = row[header['Weight (%)']].strip()
        if not ticker or not weight or ticker.lower() in {'issuer ticker', 'cash'}:
            continue
        market_value = row[header.get('Market Value', header['Weight (%)'])]
        numeric_value = re.sub(r'[^0-9.+-]', '', market_value)
        if not numeric_value:
            continue
        holdings.append({'symbol': ticker, 'name': row[header['Name']],
                         'weight': float(number(weight) / 100),
                         'value': float(number(numeric_value)),
                         'country': default_country,
                         'sector': sector(row[header['Sector']]),
                         'equity': row[header['Asset Class']].strip().casefold() == 'equity',
                         'source_asset_class': row[header['Asset Class']]})
    if not holdings:
        raise ValueError('BlackRock XML holdings empty')
    return {'as_of_date': date_value, 'holdings': holdings, 'complete_holdings': True,
            'sector_taxonomy': 'GICS', 'country_basis': 'nested fund underlying issuer market',
            'denominator': 'equity market value', 'issues': []}


def blackrock(entry, source, fetch):
    result = parse_blackrock(fetch.get(source['url']))
    # A nested fund is expanded through its official holdings file. Its own
    # domicile and issuer sector are never used as look-through exposure.
    for nested in source.get('nested_funds', []):
        parent = next((h for h in result['holdings'] if h['symbol'] == nested), None)
        if parent is None:
            continue
        nested_url = source.get('nested_source_urls', {}).get(nested)
        if not nested_url:
            parent['country'] = parent['sector'] = None
            result['issues'].append(f'Nested fund {nested} requires look-through')
            continue
        nested_result = parse_blackrock_xml(fetch.get(nested_url))
        expanded = []
        for holding in result['holdings']:
            if holding is not parent:
                expanded.append(holding)
        for child in nested_result['holdings']:
            expanded.append({**child,
                             'symbol': f'{nested}:{child["symbol"]}',
                             'weight': child['weight'] * parent['weight'],
                             'value': child['value'] * parent['weight']})
        result['holdings'] = expanded
        result['as_of_date'] = min(result['as_of_date'], nested_result['as_of_date'])
        result['issues'].append(f'Nested fund {nested} expanded from official holdings')
    return result


def classification_map(fetch):
    mapping, dates = {}, []
    for ticker, product in [('EWT','239686/ishares-msci-taiwan-etf'),('EEMS','239642/ishares-msci-emerging-markets-smallcap-etf')]:
        doc = parse_blackrock(fetch.get(f'https://www.ishares.com/us/products/{product}/latest-holdings.csv'))
        dates.append(doc['as_of_date'])
        for holding in doc['holdings']:
            if holding['equity'] and holding['country'] == 'TW' and holding['sector']:
                mapping.setdefault(holding['symbol'], holding['sector'])
    overrides_path = ROOT / 'config' / 'taiwan-sector-overrides.json'
    if overrides_path.exists():
        overrides = json.loads(overrides_path.read_text(encoding='utf-8'))
        mapping.update({str(k): v['sector'] if isinstance(v, dict) else v for k, v in overrides.items()})
    return mapping, dates


def taiwan_sector_taxonomy(lookups):
    if any(r['status'] == 'resolved_crosswalk' for r in lookups):
        return 'GICS issuer lookup plus corroborated third-party sector crosswalk (see classification_lookup)'
    return 'GICS (issuer holdings lookup)'


def yuanta(entry, source, fetch):
    html = fetch.get(source['url'])
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node.js is required to parse Yuanta Nuxt state')
    result = subprocess.run([node, str(ROOT/'scripts/nuxt-data.cjs')], input=html,
                            capture_output=True, timeout=10, check=True)
    data = json.loads(result.stdout)
    fund = next(d['fundData'] for d in data['data'] if d.get('fundData'))
    if fund['STK_CD'] != entry['ticker']:
        raise ValueError('Yuanta identity mismatch')
    raw = next(d['weightData'] for d in data['data'] if d.get('weightData'))
    weights = raw['FundWeights']
    nav = number(raw['PCF']['totalav'])
    if nav <= 0:
        raise ValueError('Invalid NAV')
    mapping, dates = classification_map(fetch)
    holdings = [{'symbol':h['code'], 'name':h['name'], 'weight':float(number(h['weights'])/100),
                 'value':float(number(h['weights'])), 'country':'TW', 'sector':mapping.get(h['code']), 'equity':True}
                for h in weights['StockWeights']]
    lookups, extra_dates = lookup_missing(holdings, fetch, vanguard)
    dates.extend(extra_dates)
    return {'as_of_date':parse_date(raw['PCF']['trandate']), 'holdings':holdings,
            'classification_lookup': lookups,
            'isin':fund['ISINCODE'], 'name':fund['FUND_NAME'], 'complete_holdings':True,
            'classification_dates':dates, 'sector_taxonomy':taiwan_sector_taxonomy(lookups),
            'country_basis':'Taiwan domestic equity investment market; not issuer domicile',
            'equity_fraction_nav':float(number(weights['Summary']['stkvalues'])/nav),
            'denominator':'sum of issuer rounded stock weights (equity sleeve)',
            'issues':['Non-equity NAV residual remains unclassified; futures notional excluded']}


def fubon(entry, source, fetch):
    soup = BeautifulSoup(fetch.get(source['url']), 'html.parser')
    text = soup.get_text(' ', strip=True)
    if entry['ticker'] not in text:
        raise ValueError('Fubon identity missing')
    date_match = re.search(r'(?:日期|資料日|資料日期|淨值日期)[：:\s]*(\d{4}[/-]\d{1,2}[/-]\d{1,2})', text)
    if not date_match:
        raise ValueError('Cannot identify Fubon holdings date')
    nav = number(re.search(r'基金淨資產\(新台幣\)\s*([\d,]+)', text)[1])
    table = next(t for t in soup.find_all('table') if '股票代碼' in t.get_text() and '金額' in t.get_text())
    mapping, dates = classification_map(fetch)
    holdings = []
    for row in table.find_all('tr'):
        cells = [c.get_text(' ',strip=True) for c in row.find_all(['td','th'])]
        if len(cells) >= 5 and re.fullmatch(r'\d{4}', cells[0]):
            value = number(cells[3])
            holdings.append({'symbol':cells[0], 'name':cells[1], 'value':float(value),
                             'weight':float(value/nav), 'country':'TW', 'sector':mapping.get(cells[0]), 'equity':True})
    if not holdings:
        raise ValueError('Empty Fubon stock table')
    lookups, extra_dates = lookup_missing(holdings, fetch, vanguard)
    dates.extend(extra_dates)
    return {'as_of_date':parse_date(date_match[1]), 'holdings':holdings, 'complete_holdings':True,
            'classification_lookup': lookups,
            'classification_dates':dates, 'sector_taxonomy':taiwan_sector_taxonomy(lookups),
            'country_basis':'Taiwan domestic equity investment market; not issuer domicile',
            'denominator':'equity market value',
            'equity_fraction_nav':sum(h['weight'] for h in holdings),
            'issues':['Non-equity NAV residual remains unclassified; futures notional excluded']}


def vanguard(entry, source, fetch):
    request = json.loads((ROOT/'config/vanguard-query.json').read_text(encoding='utf-8'))
    if source.get('holdings_field') == 'delayeredHoldings':
        request['query'] = request['query'].replace('holdings(limit:', 'delayeredHoldings(limit:')
    request['variables']['portIds'] = [source['portfolio_id']]
    request['variables']['lastItemKey'] = None
    request['variables']['securityTypes'] = source.get('security_types')
    items, cursors, dates = [], set(), set()
    for _ in range(40):
        request_headers = {}
        if source.get('consumer_id'):
            request_headers = {'X-Consumer-ID': source['consumer_id'],
                               'apollographql-client-name': 'gpx'}
        response = json.loads(fetch.get(source['url'], request, headers=request_headers) if request_headers
                              else fetch.get(source['url'], request))
        if response.get('errors'):
            raise ValueError(f"Vanguard API errors: {response['errors']}")
        profile = response['data']['funds'][0]['profile']
        if source['name_contains'].casefold() not in profile['fundFullName'].casefold():
            raise ValueError('Vanguard identity mismatch')
        field = source.get('holdings_field','holdings')
        page = response['data']['borHoldings'][0][field]
        items.extend(page['items'])
        cursor = page['lastItemKey']
        if not cursor:
            break
        if cursor in cursors:
            raise ValueError('Repeated holdings cursor')
        cursors.add(cursor)
        request['variables']['lastItemKey'] = cursor
    else:
        raise ValueError('Holdings pagination limit')
    if not items or len(items) != page['totalHoldings']:
        raise ValueError(f"Incomplete holdings: {len(items)} / {page['totalHoldings']}")
    holdings = []
    for h in items:
        dates.add(parse_date(h['effectiveDate']))
        holdings.append({'symbol':h.get('ticker') or '', 'name':h['securityLongDescription'],
                         'weight':float(number(h['marketValuePercentage'])/100),
                         'value':float(number(h['marketValueBaseCurrency'])),
                         'country':country(h['bloombergIsoCountry']), 'sector':sector(h['gicsSectorDescription']),
                         'equity':h['securityType'] == 'EQ.STOCK'})
    if len(dates) != 1:
        raise ValueError('Mixed Vanguard holdings dates')
    return {'as_of_date':dates.pop(), 'name':profile['fundFullName'], 'holdings':holdings,
            'complete_holdings':True, 'sector_taxonomy':'GICS (gicsSectorDescription, not ICB)',
            'country_basis':'bloombergIsoCountry', 'denominator':'equity market value', 'issues':[]}


def invesco(entry, source, fetch):
    """Read Invesco's official UCITS holdings and aggregate allocation APIs."""
    holdings_doc = json.loads(fetch.get(source['url']))
    isin = source.get('isin') or entry.get('isin')
    if holdings_doc.get('isin') and isin and holdings_doc['isin'] != isin:
        raise ValueError('Invesco identity mismatch')
    as_of = parse_date(holdings_doc.get('effectiveDate'))
    rows = holdings_doc.get('holdings')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Empty Invesco holdings')
    holdings = []
    for row in rows:
        if not isinstance(row, dict) or 'weight' not in row or 'name' not in row:
            raise ValueError('Malformed Invesco holding')
        weight = number(row['weight']) / 100
        if weight < 0:
            raise ValueError('Negative Invesco holding weight')
        security_isin = row.get('isin')
        holdings.append({'symbol': row.get('ticker') or None, 'isin': security_isin,
                         'cusip': row.get('cusip'), 'name': row['name'],
                         'weight': float(weight), 'value': float(weight),
                         'country': None, 'sector': None,
                         'equity': bool(security_isin)})
    total_weight = sum(number(h['weight']) for h in holdings)
    if total_weight <= 0 or total_weight > number('1.005'):
        raise ValueError(f'Invalid Invesco holdings total: {total_weight}')

    sector_names = {
        'informationTechnology': 'Information Technology',
        'financials': 'Financials', 'industrials': 'Industrials',
        'consumerDiscretionary': 'Consumer Discretionary',
        'healthCare': 'Health Care', 'communicationServices': 'Communication Services',
        'consumerStaples': 'Consumer Staples', 'energy': 'Energy',
        'materials': 'Materials', 'utilities': 'Utilities', 'realEstate': 'Real Estate',
        'other': 'Other',
    }
    country_names = {
        'UnitedStates': 'US', 'Japan': 'JP', 'Taiwan': 'TW', 'Canada': 'CA',
        'China': 'CN', 'SouthKorea': 'KR', 'UnitedKingdom': 'GB',
        'Germany': 'DE', 'Australia': 'AU', 'Other': 'Other',
    }

    def allocation(url, names, label):
        document = json.loads(fetch.get(url))
        if document.get('isin') and isin and document['isin'] != isin:
            raise ValueError(f'Invesco {label} identity mismatch')
        if parse_date(document.get('effectiveDate')) != as_of:
            raise ValueError(f'Invesco {label}/holdings dates differ')
        result = {}
        for item in document.get('holdingWeights', []):
            key = item.get('name')
            if key not in names:
                raise ValueError(f'Unmapped Invesco {label}: {key!r}')
            value = number(item.get('value')) / 100
            if value < 0 or key in result:
                raise ValueError(f'Invalid Invesco {label} allocation')
            result[names[key]] = float(value)
        if not result or sum(number(v) for v in result.values()) <= 0:
            raise ValueError(f'Empty Invesco {label} allocation')
        return result

    sectors = allocation(source['sector_url'], sector_names, 'sector')
    countries = allocation(source['country_url'], country_names, 'country')
    residual = sum(h['weight'] for h in holdings if not h['equity'])
    issues = []
    if residual:
        issues.append(f'Cash and/or derivatives excluded from equity denominator: {residual:.4%}')
    return {'as_of_date': as_of, 'name': entry['name'], 'isin': isin,
            'holdings': holdings, 'complete_holdings': True,
            'direct_sectors': sectors, 'direct_countries': countries,
            'classification_dates': [as_of],
            'sector_taxonomy': 'Issuer published sector allocation (GICS buckets; Other retained)',
            'country_basis': 'Issuer published country allocation (Other retained)',
            'denominator': 'issuer holding weights; non-equity residual excluded',
            'equity_fraction_nav': float(sum(h['weight'] for h in holdings if h['equity'])),
            'issues': issues}


def ssga(entry, source, fetch):
    workbook = load_workbook(io.BytesIO(fetch.get(source['url'])), read_only=True, data_only=True)
    rows = list(workbook.active.values)
    header = next(i for i,r in enumerate(rows) if 'Weight' in r and 'Name' in r)
    columns = {v:i for i,v in enumerate(rows[header]) if v}
    dates = []
    for row in rows[:header]:
        for value in row:
            if isinstance(value, datetime):
                dates.append(value.date().isoformat())
            elif isinstance(value,str):
                match = re.search(r'(?:as of|As of|As Of)\s*[:：]?\s*(.+)',value)
                if match:
                    try:
                        dates.append(parse_date(match[1]))
                    except ValueError:
                        pass
    if not dates:
        raise ValueError('SSGA holdings date missing')
    holdings = []
    for row in rows[header+1:]:
        weight = row[columns['Weight']]
        if not isinstance(weight,(int,float)):
            continue
        def cell(*names):
            return next((row[columns[n]] for n in names if n in columns), None)
        holdings.append({'symbol':cell('Ticker'), 'name':cell('Name'), 'weight':float(number(weight)/100),
                         'value':float(number(weight)), 'sector':sector(cell('Sector')),
                         'country':country(cell('Country','Country of Risk')),
                         'equity':not bool(re.search(r'\bFUT\b|\bCASH\b', str(cell('Name')),re.I))})
        if source.get('country_override') and holdings[-1]['equity']:
            holdings[-1]['country'] = source['country_override']
    result = {'as_of_date':dates[0], 'holdings':holdings, 'complete_holdings':True,
            'sector_taxonomy':'GICS (issuer Sector)', 'country_basis':'issuer country, unknown if absent',
            'denominator':'sum of stock holding weights', 'issues':[]}
    if source.get('allocation_url'):
        soup = BeautifulSoup(fetch.get(source['allocation_url']), 'html.parser')
        section = next(s for s in soup.find_all('section') if s.find('h2') and 'Sector Allocation' in s.find('h2').get_text())
        allocation_date = parse_date(section.find(class_='date').get_text(strip=True).replace('as of ',''))
        if allocation_date != result['as_of_date']:
            raise ValueError('SSGA allocation/holdings dates differ')
        table = section.find('table')
        allocation = {}
        for tr in table.find_all('tr'):
            cells = tr.find_all('td')
            if len(cells) >= 2:
                label = sector(cells[0].get_text(strip=True))
                if not label:
                    raise ValueError('Unmapped SSGA sector label')
                allocation[label] = float(number(cells[1].get_text(strip=True))/100)
        if len(allocation) != 11:
            raise ValueError('Incomplete SSGA sector table')
        result['direct_sectors'] = allocation
        if source.get('country_override'):
            result['country_basis'] = 'S&P 500 constituent universe (US); issuer holdings file has no country column'
        else:
            result['issues'].append('Country allocation unavailable in issuer holdings file')
    return result


def metadata(entry, source, fetch):
    content = fetch.get(source['url'])
    if source.get('content_type') == 'html':
        page = BeautifulSoup(content, 'html.parser').get_text(' ', strip=True)
        date_match = re.search(r'(?:At closure|as of)\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})', page)
        as_of = parse_date(date_match.group(1)) if date_match else source.get('as_of_date')
    else:
        as_of = source.get('as_of_date')
    if not as_of:
        raise ValueError('Metadata source date missing')
    return {'metadata_only': True, 'as_of_date': as_of, 'name': entry['name'],
            'isin': source.get('isin'), 'asset_class': source.get('asset_class', 'Equity'),
            'domicile': source.get('domicile'), 'source_description': source.get('description'),
            'issues': ['Official holdings allocation unavailable at source'],
            'sector_taxonomy': 'Unavailable', 'country_basis': 'Unavailable'}


def evidence_only(entry, source, fetch):
    fetch.get(source['url'])
    raise ValueError(source['reason'])


ADAPTERS = {'yuanta':yuanta, 'fubon':fubon, 'blackrock':blackrock,
            'vanguard':vanguard, 'invesco':invesco, 'ssga':ssga,
            'metadata': metadata, 'moneydj_fund': moneydj_fund,
            'evidence_only':evidence_only}
