"""Resolve missing Taiwan equity sectors from dated official sources.

Exchange industry codes are evidence for review, never an implicit GICS map.
"""
import json
import re
from datetime import date

from common import sector
from sector_providers import provider_urls

VANGUARD_SOURCE = {
    'url': 'https://www.vanguard.co.uk/gpx/graphql',
    'portfolio_id': '3141',
    'name_contains': 'Vanguard Total World Stock ETF',
}
EXCHANGES = [
    ('TWSE', 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L',
     ('公司代號', '公司名稱', '產業別', '出表日期')),
    ('TPEx', 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O',
     ('SecuritiesCompanyCode', 'CompanyName', 'SecuritiesIndustryCode', 'Date')),
]


def exchange_records(content, fields):
    rows = json.loads(content)
    if not isinstance(rows, list) or not rows:
        raise ValueError('Empty or malformed exchange company data')
    code, name, industry, dated = fields
    result = {}
    for row in rows:
        if not isinstance(row, dict) or any(key not in row for key in fields):
            raise ValueError('Exchange company schema changed')
        symbol = str(row[code]).strip()
        stamp = str(row[dated]).strip()
        if not re.fullmatch(r'\d{7}', stamp):
            raise ValueError('Invalid exchange ROC date')
        as_of = date(int(stamp[:3]) + 1911, int(stamp[3:5]), int(stamp[5:])).isoformat()
        if symbol in result:
            raise ValueError('Duplicate exchange company code')
        result[symbol] = {'company_name': str(row[name]).strip(),
                          'industry_code': str(row[industry]).strip(), 'as_of_date': as_of}
    return result


def _fresh(stamp, max_age_days):
    if not stamp:
        return False
    return 0 <= (date.today() - date.fromisoformat(stamp[:10])).days <= max_age_days


def lookup_secondary(record, fetch, max_age_days):
    """Keep searching after issuer misses; require corroboration to apply a crosswalk."""
    if not re.fullmatch(r'\d{4}', record['symbol']):
        record['decision'] = 'Unsupported symbol format for secondary providers; no sector applied'
        return []
    companies = [(a['source'], a['company']) for a in record['attempts'] if a.get('company')]
    markets = [market for market, _ in companies] or ['TWSE', 'TPEx']
    candidates = []
    for market in markets:
        for name, url, parser in provider_urls(record['symbol'], market):
            start = len(fetch.evidence)
            attempt = {'source': name, 'source_url': url, 'market': market}
            try:
                candidate = parser(fetch.get(url), record['symbol'], market, url)
                evidence = fetch.evidence[start:]
                attempt.update(candidate=candidate, evidence=evidence)
                # A new download must not rejuvenate an old/undated profile.
                # Undated TradingView evidence only corroborates a dated profile.
                observed = evidence[-1].get('fetched_at') if evidence else None
                candidate['eligible'] = (_fresh(candidate.get('as_of_date'), max_age_days)
                                         if name == 'Stock Analysis' else _fresh(observed, max_age_days))
                candidates.append((name, market, candidate))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                attempt.update(error=f'{type(exc).__name__}: {exc}', evidence=fetch.evidence[start:])
            record['attempts'].append(attempt)
    record['candidates'] = [{'source': name, 'market': market, **candidate}
                            for name, market, candidate in candidates]
    eligible = [(name, market, c) for name, market, c in candidates if c['eligible'] and c.get('sector')]
    sectors = {c['sector'] for _, _, c in eligible}
    if record['status'] == 'conflict' or len(sectors) > 1:
        record.update(status='conflict', decision='Conflicting classifications; no sector applied')
        return []
    for market, company in companies:
        matches = [(name, c) for name, m, c in eligible if m == market]
        isins = {c.get('isin') for _, c in matches}
        if (len({name for name, _ in matches}) >= 2 and len(isins) == 1
                and next(iter(isins)) and re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d', next(iter(isins)))
                and _fresh(company['as_of_date'], max_age_days)):
            record.update(status='resolved_crosswalk', sector=next(iter(sectors)),
                          decision='Dated profile and corroborating sector/industry crosswalk agree; market and ISIN match',
                          taxonomy='Third-party sector crosswalk; not issuer-native GICS')
            return [c['as_of_date'] for _, c in matches if c.get('as_of_date')]
    record.update(status='candidate' if candidates else 'unresolved',
                  decision='Insufficient dated, matching sector evidence; no sector applied')
    return []


def lookup_missing(holdings, fetch, load_vanguard, max_age_days=90):
    missing = {h['symbol'] for h in holdings
               if h['equity'] and h['country'] == 'TW' and not h.get('sector')}
    if not missing:
        return [], []
    records = {symbol: {'symbol': symbol, 'status': 'unresolved', 'attempts': []}
               for symbol in sorted(missing)}
    dates = []
    try:
        start = len(fetch.evidence)
        doc = load_vanguard({}, VANGUARD_SOURCE, fetch)
        age = (date.today() - date.fromisoformat(doc['as_of_date'])).days
        if not 0 <= age <= max_age_days:
            raise ValueError('Stale or future Vanguard classification date')
        matches = {}
        for holding in doc['holdings']:
            if holding['equity'] and holding['country'] == 'TW' and sector(holding.get('sector')):
                matches.setdefault(holding['symbol'], set()).add(holding['sector'])
        for symbol, record in records.items():
            candidates = matches.get(symbol, set())
            record['attempts'].append({'source': 'Vanguard VT official GICS',
                                       'source_url': VANGUARD_SOURCE['url'],
                                       'as_of_date': doc['as_of_date'],
                                       'sectors': sorted(candidates),
                                       'evidence': fetch.evidence[start:]})
            if len(candidates) == 1:
                record.update(status='resolved', sector=next(iter(candidates)))
            elif candidates:
                record['status'] = 'conflict'
        if any(r['status'] == 'resolved' for r in records.values()):
            dates.append(doc['as_of_date'])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        for record in records.values():
            record['attempts'].append({'source': 'Vanguard VT official GICS',
                                       'source_url': VANGUARD_SOURCE['url'],
                                       'error': f'{type(exc).__name__}: {exc}'})
    pending = {s for s, r in records.items() if r['status'] != 'resolved'}
    for exchange, url, fields in EXCHANGES:
        if not pending:
            break
        try:
            start = len(fetch.evidence)
            companies = exchange_records(fetch.get(url), fields)
            for symbol in sorted(pending):
                company = companies.get(symbol)
                records[symbol]['attempts'].append({
                    'source': exchange, 'source_url': url, 'company': company,
                    'taxonomy': 'Exchange industry code; not GICS',
                    'evidence': fetch.evidence[start:],
                })
                if company:
                    pending.remove(symbol)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            for symbol in sorted(pending):
                records[symbol]['attempts'].append({'source': exchange, 'source_url': url,
                                                   'error': f'{type(exc).__name__}: {exc}'})
    for record in records.values():
        if record['status'] != 'resolved':
            dates.extend(lookup_secondary(record, fetch, max_age_days))
    for holding in holdings:
        record = records.get(holding['symbol'])
        if (record and record['status'] in ('resolved', 'resolved_crosswalk') and holding['equity']
                and holding['country'] == 'TW' and not holding.get('sector')):
            holding['sector'] = record['sector']
    return list(records.values()), dates
