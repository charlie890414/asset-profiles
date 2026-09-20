"""Build review candidates, never mutate Wealthfolio or published profiles."""
from __future__ import annotations

import argparse
import copy
import json
from datetime import date, timedelta
from pathlib import Path

from common import ROOT, basis_points, digest, now, number, safe_key, validate_profile, write_json
from sources import ADAPTERS, Fetcher


def classify(entry, raw, evidence, max_age_days=90):
    as_of = date.fromisoformat(raw['as_of_date'])
    age = (date.today() - as_of).days
    if age < 0:
        raise ValueError('Future holdings date')
    if raw.get('metadata_only'):
        profile = {'schema_version': '1.0.0', 'kind': 'etf',
                   'primary_symbol': entry['symbol'], 'listings': entry['listings'],
                   'name': raw.get('name', entry['name']), 'issuer': entry['issuer'],
                   'as_of_date': raw['as_of_date'],
                   'provenance': {'source': entry['issuer'] + ' official',
                                  'source_url': evidence[0]['source_url'],
                                  'fetched_at': evidence[0]['fetched_at'],
                                  'license': 'Issuer terms; rights not assumed'},
                   'asset_class_weights': [{'asset_class': raw.get('asset_class', 'Equity'), 'weight': 1.0}]}
        if raw.get('isin') or entry.get('isin'):
            profile['isin'] = raw.get('isin', entry.get('isin'))
        validate_profile(profile)
        return {'profile': profile, 'partial_allocations': {},
                'metadata': {'allocations': {}, 'sector_taxonomy': raw['sector_taxonomy'],
                             'country_basis': raw['country_basis'], 'classification_dates': [],
                             'equity_fraction_nav': 1.0, 'confidence': 55,
                             'evidence': evidence, 'reasons': raw.get('issues', []),
                             'scope': 'Official fund metadata only; underlying allocation unavailable',
                             'asset_class_status': 'official metadata; sector/country pending'}}
    holdings = raw['holdings']
    if not holdings:
        raise ValueError('No holdings')
    if any(number(h['value']) < 0 or number(h['weight']) < 0 for h in holdings if h['equity']):
        raise ValueError('Signed positions require derivative-aware classification')
    equity = [h for h in holdings if h['equity']]
    denominator = sum(number(h['value']) for h in equity)
    if denominator <= 0:
        raise ValueError('No positive equity denominator; do not infer GICS')
    profile = {'schema_version':'1.0.0', 'kind':'etf', 'primary_symbol':entry['symbol'],
               'listings':entry['listings'], 'name':raw.get('name', entry['name']),
               'issuer':entry['issuer'], 'as_of_date':raw['as_of_date'],
               'provenance':{'source':entry['issuer']+' official', 'source_url':evidence[0]['source_url'],
                             'fetched_at':evidence[0]['fetched_at'], 'license':'Issuer terms; rights not assumed'}}
    if raw.get('isin') or entry.get('isin'):
        profile['isin'] = raw.get('isin', entry.get('isin'))
    partial, metadata, reasons = {}, {}, list(raw.get('issues', []))
    classification_dates = raw.get('classification_dates', [])
    if any((date.today()-date.fromisoformat(d)).days > max_age_days for d in classification_dates):
        reasons.append('Stale security classification evidence')
    if age > max_age_days:
        reasons.append(f'Stale holdings: {age} days')
    for field, label, value_key in [('sector_weights','sector','sector'),('country_weights','country','country')]:
        direct = raw.get('direct_sectors') if field == 'sector_weights' else raw.get('direct_countries')
        if direct is not None:
            grouped = direct
            coverage = sum(number(v) for v in grouped.values())
            if abs(coverage-1) > number('.005'):
                raise ValueError(f'Incomplete direct {label} allocation')
            complete, missing = True, []
            bp = basis_points(grouped, normalize=True)
            rows = []
            for key, weight in sorted(bp.items(), key=lambda x: (-x[1], x[0])):
                if not weight:
                    continue
                row = {label: key, 'weight': weight / 10000}
                if label == 'country':
                    row['country_code'] = None if key == 'Other' else key
                rows.append(row)
            partial[field] = rows
            metadata[field] = {'coverage': float(coverage), 'unallocated_basis_points': 0,
                               'missing_securities': [], 'complete': complete,
                               'denominator': 'issuer published sector allocation' if label == 'sector' else 'issuer published country allocation',
                               'denominator_value': str(denominator)}
            profile[field] = rows
            continue
        grouped = {}
        missing = []
        for h in equity:
            value = h[value_key]
            if value:
                grouped[value] = grouped.get(value, 0) + number(h['value']) / denominator
            else:
                missing.append(h.get('symbol') or h['name'])
        coverage = sum(grouped.values())
        unclassified_weight = sum(number(h['value']) for h in equity if not h[value_key]) / denominator
        # Tiny issuer rounding omissions (<=2 bp) do not warrant blocking the
        # whole profile; the unallocated amount is retained in review metadata.
        complete = raw['complete_holdings'] and (not missing or unclassified_weight <= number('0.0002'))
        bp = basis_points(grouped, normalize=complete) if grouped else {}
        rows = [{label:key, 'weight':weight/10000, **({'country_code':key} if label == 'country' else {})}
                for key,weight in sorted(bp.items(), key=lambda x:(-x[1],x[0])) if weight]
        partial[field] = rows
        metadata[field] = {'coverage':float(coverage), 'unallocated_basis_points':10000-sum(bp.values()),
                           'missing_securities':missing, 'complete':complete,
                           'denominator':'issuer published sector allocation' if field == 'sector_weights' and raw.get('direct_sectors') else raw['denominator'], 'denominator_value':str(denominator)}
        if complete:
            profile[field] = rows
        else:
            reasons.append(f'{field}: incomplete coverage {float(coverage):.4%}')
    profile['holdings_count'] = len(holdings)
    top = sorted(equity, key=lambda h:h['weight'], reverse=True)[:10]
    if sum(number(h['weight']) for h in top) <= number('1.005'):
        profile['top_holdings'] = [{k:h.get(k) for k in ('symbol', 'isin', 'cusip', 'name', 'weight')
                                   if h.get(k) is not None} for h in top]
    else:
        reasons.append('Top holding weights exceed NAV tolerance')
    validate_profile(profile)
    coverage = min(m['coverage'] for m in metadata.values())
    confidence = max(0, min(95, int(95 - age//7 - (1-coverage)*100)))
    if confidence < 80:
        reasons.append('Low confidence')
    return {'profile':profile, 'partial_allocations':partial,
            'metadata':{'allocations':metadata, 'sector_taxonomy':raw['sector_taxonomy'],
                        'country_basis':raw['country_basis'], 'classification_dates':classification_dates,
                        'equity_fraction_nav':raw.get('equity_fraction_nav'), 'confidence':confidence,
                        'evidence':evidence, 'reasons':reasons,
                        'classification_lookup':raw.get('classification_lookup', []),
                        'scope':'underlying equity exposure; not the ETF legal entity GICS',
                        'asset_class_status':'requires separate NAV/component reconciliation'}}


def classify_fund(entry, raw, evidence, max_age_days=90):
    """Build a fund profile from reported NAV allocation and top holdings.

    Mutual-fund platforms commonly publish a complete asset-class split but only
    a monthly top-ten look-through.  Keep those denominators explicit and leave
    GICS/country fields absent when the source does not publish a complete
    look-through distribution.
    """
    as_of = date.fromisoformat(raw['as_of_date'])
    age = (date.today() - as_of).days
    if age < 0:
        raise ValueError('Future fund allocation date')
    weights = raw.get('asset_class_weights') or raw.get('fund_asset_class_weights')
    if not isinstance(weights, dict) or not weights:
        raise ValueError('Fund asset-class allocation missing')
    bp = basis_points(weights, normalize=True)
    asset_rows = [{'asset_class': key, 'weight': value / 10000}
                  for key, value in sorted(bp.items(), key=lambda item: (-item[1], item[0])) if value]
    profile = {'schema_version': '1.0.0', 'kind': 'fund',
               'primary_symbol': entry['symbol'], 'listings': entry['listings'],
               'name': raw.get('name', entry['name']), 'issuer': entry['issuer'],
               'as_of_date': raw['as_of_date'],
               'provenance': {'source': raw.get('source_name', entry['issuer'] + ' official'),
                              'source_url': evidence[0]['source_url'],
                              'fetched_at': evidence[0]['fetched_at'],
                              'license': 'Issuer/platform terms; rights not assumed'},
               'asset_class_weights': asset_rows}
    if raw.get('isin') or entry.get('isin'):
        profile['isin'] = raw.get('isin', entry.get('isin'))
    notes = list(raw.get('issues', []))
    if not raw.get('complete_holdings', False):
        notes.append('Source publishes monthly top holdings only; this is not a complete look-through.')
    if notes:
        profile['classification_notes'] = notes
    top_holdings = raw.get('top_holdings') or []
    if top_holdings and sum(number(h['weight']) for h in top_holdings) <= number('1.005'):
        profile['top_holdings'] = [{key: holding[key] for key in ('symbol', 'isin', 'name', 'weight')
                                    if holding.get(key) is not None}
                                   for holding in top_holdings[:10]]
    elif top_holdings:
        notes.append('Top holding weights exceed NAV tolerance')
    validate_profile(profile)
    reasons = notes
    if age > max_age_days:
        reasons.append(f'Stale fund allocation: {age} days')
    confidence = max(0, min(95, int(90 - age // 14)))
    if not raw.get('complete_holdings', False):
        confidence = min(confidence, 85)
    if confidence < 80:
        reasons.append('Low confidence')
    metadata = {'allocations': {'asset_class_weights': {
                    'coverage': 1.0, 'unallocated_basis_points': 0,
                    'missing_securities': [], 'complete': True,
                    'denominator': raw.get('asset_class_basis', 'reported fund NAV allocation'),
                    'denominator_value': '1.0'}},
                'sector_taxonomy': raw.get('sector_taxonomy', 'Unavailable'),
                'country_basis': raw.get('country_basis', 'Unavailable'),
                'classification_dates': raw.get('classification_dates', [raw['as_of_date']]),
                'equity_fraction_nav': raw.get('equity_fraction_nav'),
                'confidence': confidence, 'evidence': evidence, 'reasons': reasons,
                'scope': 'fund NAV asset allocation; look-through fields only when complete',
                'asset_class_status': 'reported fund NAV allocation'}
    return {'profile': profile, 'partial_allocations': {}, 'metadata': metadata}


def substantive(profile):
    result = copy.deepcopy(profile)
    result.get('provenance', {}).pop('fetched_at', None)
    return result


def build(universe, output, published, fetch, symbols=None):
    reports = []
    seen = set()
    entries = [(entry, 'etf') for entry in universe.get('etfs', [])]
    entries += [(entry, 'fund') for entry in universe.get('funds', [])]
    for entry, kind in entries:
        key = safe_key(entry['symbol'])
        if key in seen:
            raise ValueError('Duplicate universe symbol')
        seen.add(key)
        if symbols and key not in symbols:
            continue
        directory = 'funds' if kind == 'fund' else 'etfs'
        old_path = published/directory/f'{key}.json'
        old = json.loads(old_path.read_text(encoding='utf-8')) if old_path.exists() else None
        attempts = []
        candidate = None
        for source in entry['sources']:
            start = len(fetch.evidence)
            try:
                raw = ADAPTERS[source['adapter']](entry, source, fetch)
                classifier = classify_fund if kind == 'fund' else classify
                candidate = classifier(entry, raw, fetch.evidence[start:], universe.get('max_age_days',90))
                break
            except Exception as error:
                attempts.append({'adapter':source['adapter'], 'url':source['url'], 'error':type(error).__name__+': '+str(error),
                                 'evidence':fetch.evidence[start:]})
        review_path = output/'drafts'/f'{key}.json'
        if candidate is None:
            # Replace stale pending proposals with an error; leave published data alone.
            draft = {'symbol':key, 'status':'source_error', 'attempts':attempts, 'profile':None}
        else:
            new = candidate['profile']
            change = old is None or substantive(old) != substantive(new)
            override = ROOT/'manual_overrides'/f'{key}.json'
            reasons = candidate['metadata']['reasons']
            if override.exists():
                reasons.append('Manual override lock: explicit review required')
            if old and old.get('as_of_date','') > new.get('as_of_date',''):
                reasons.append('Source date regressed; cannot promote')
            removed = sorted(set(old or {}) - set(new))
            if removed:
                reasons.append('Fields would be removed: '+', '.join(removed))
            changed_fields = sorted(k for k in set(old or {}) | set(new) if (old or {}).get(k) != new.get(k) and k != 'provenance')
            allocation_meta = candidate['metadata']['allocations']
            metadata_only = not allocation_meta
            complete_candidate = (not metadata_only and all(m.get('complete') for m in allocation_meta.values())
                                  and candidate['metadata']['confidence'] >= 80
                                  and not any('incomplete coverage' in reason or 'Stale' in reason or 'Low confidence' in reason
                                              for reason in candidate['metadata']['reasons']))
            status = 'metadata_only' if metadata_only else ('ready' if change and complete_candidate else
                     'no_change' if not change else 'needs_review')
            draft = {'symbol':key, 'status':status,
                     'base_sha256':digest(old), 'candidate_sha256':digest(new),
                     'changed_fields':changed_fields, 'previous':old, 'attempts':attempts, **candidate}
        write_json(review_path, draft)
        reports.append({'symbol':key, 'status':draft['status'],
                        'as_of_date':draft.get('profile',{}).get('as_of_date') if draft.get('profile') else None,
                        'reasons':draft.get('metadata',{}).get('reasons',[]), 'attempts':attempts,
                        'classification_lookup':draft.get('metadata',{}).get('classification_lookup', [])})
        print(f"{key}: {draft['status']}", flush=True)
    if not reports:
        raise ValueError('No matching ETFs or funds')
    write_json(output/'summary.json', {'generated_at':now(), 'results':reports})
    markdown = ['# ETF／基金更新審核', '', '所有資料僅建立審核草稿，未寫入 Wealthfolio。基金若只有前十大持股，會保留此限制。', '',
                '| 資產 | 狀態 | 資料日期 | 原因 |', '|---|---|---|---|']
    for r in reports:
        notes = '; '.join(r['reasons'] + [a['error'] for a in r['attempts']]).replace('|','/').replace('\n',' ')
        markdown.append(f"| {r['symbol']} | {r['status']} | {r['as_of_date'] or '-'} | {notes} |")
    lookups = [(r['symbol'], lookup) for r in reports for lookup in r['classification_lookup']]
    if lookups:
        markdown.extend(['', '## 缺漏產業自動查找', '',
                         '缺漏依序查 Vanguard、交易所身分、Stock Analysis 與 TradingView。第三方分類對照明確標示為 crosswalk；交易所產業代碼不直接轉成 GICS。完整證據見草稿 metadata.classification_lookup。', '',
                         '| ETF | 持股 | 結果 | 查找證據 |', '|---|---|---|---|'])
        for symbol, lookup in lookups:
            details = []
            for attempt in lookup['attempts']:
                company = attempt.get('company')
                candidate = attempt.get('candidate')
                detail = attempt.get('error') or (
                    f"{company['company_name']} / 交易所產業代碼 {company['industry_code']} / {company['as_of_date']}"
                    if company else (
                        f"{candidate['raw_sector']} / {candidate.get('industry')} → {candidate.get('sector') or '未建立對照'}"
                        f" / {candidate.get('as_of_date') or '分類日期未公開'} / eligible={candidate['eligible']}"
                        if candidate else ', '.join(attempt.get('sectors', [])) or '未找到分類'))
                details.append(f"[{attempt['source']}]({attempt['source_url']}): {detail}")
            note = '; '.join(details).replace('|', '/').replace('\n', ' ')
            markdown.append(f"| {symbol} | {lookup['symbol']} | {lookup['status']} {lookup.get('sector', '')} | {note} |")
    (output/'review.md').write_text('\n'.join(markdown)+'\n',encoding='utf-8')
    return reports


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,default=ROOT/'config/etfs.json')
    parser.add_argument('--out',type=Path,default=ROOT/'review')
    parser.add_argument('--published',type=Path,default=ROOT/'v1')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--symbols',nargs='+')
    args = parser.parse_args()
    reports = build(json.loads(args.config.read_text(encoding='utf-8')),args.out,args.published,
                    Fetcher(offline=args.offline),set(args.symbols) if args.symbols else None)
    return 1 if any(r['status']=='source_error' for r in reports) else 0


if __name__ == '__main__':
    raise SystemExit(main())
