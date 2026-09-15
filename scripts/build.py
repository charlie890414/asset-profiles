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
                        'scope':'underlying equity exposure; not the ETF legal entity GICS',
                        'asset_class_status':'requires separate NAV/component reconciliation'}}


def substantive(profile):
    result = copy.deepcopy(profile)
    result.get('provenance', {}).pop('fetched_at', None)
    return result


def build(universe, output, published, fetch, symbols=None):
    reports = []
    seen = set()
    for entry in universe['etfs']:
        key = safe_key(entry['symbol'])
        if key in seen:
            raise ValueError('Duplicate universe symbol')
        seen.add(key)
        if symbols and key not in symbols:
            continue
        old_path = published/'etfs'/f'{key}.json'
        old = json.loads(old_path.read_text(encoding='utf-8')) if old_path.exists() else None
        attempts = []
        candidate = None
        for source in entry['sources']:
            start = len(fetch.evidence)
            try:
                raw = ADAPTERS[source['adapter']](entry, source, fetch)
                candidate = classify(entry, raw, fetch.evidence[start:], universe.get('max_age_days',90))
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
                        'reasons':draft.get('metadata',{}).get('reasons',[]), 'attempts':attempts})
        print(f"{key}: {draft['status']}", flush=True)
    if not reports:
        raise ValueError('No matching ETFs')
    write_json(output/'summary.json', {'generated_at':now(), 'results':reports})
    markdown = ['# ETF 更新審核', '', '所有產業／國家配置均為股票部位曝險。未寫入 Wealthfolio。', '',
                '| ETF | 狀態 | 資料日期 | 原因 |', '|---|---|---|---|']
    for r in reports:
        notes = '; '.join(r['reasons'] + [a['error'] for a in r['attempts']]).replace('|','/').replace('\n',' ')
        markdown.append(f"| {r['symbol']} | {r['status']} | {r['as_of_date'] or '-'} | {notes} |")
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
