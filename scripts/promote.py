"""Promote explicitly selected, reviewed candidates to the static dataset."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import ROOT, digest, now, safe_key, validate_profile, write_json


def promote(symbols, review, published):
    prepared = []
    for symbol in symbols:
        key = safe_key(symbol)
        draft = json.loads((review/'drafts'/f'{key}.json').read_text(encoding='utf-8'))
        if draft['status'] != 'ready':
            raise ValueError(f'{key}: not an actionable review draft')
        profile = draft['profile']
        if profile['primary_symbol'] != key or digest(profile) != draft['candidate_sha256']:
            raise ValueError('Draft identity/hash mismatch')
        old_path = published/'etfs'/f'{key}.json'
        old = json.loads(old_path.read_text(encoding='utf-8')) if old_path.exists() else None
        if digest(old) != draft['base_sha256']:
            raise ValueError('Published data changed after draft; refresh review')
        if old and old.get('as_of_date','') > profile.get('as_of_date',''):
            raise ValueError('Refusing older source date')
        if (ROOT/'manual_overrides'/f'{key}.json').exists():
            raise ValueError(f'{key}: manual override lock must be reviewed and removed first')
        if (datetime.now(timezone.utc).date()-datetime.fromisoformat(profile['as_of_date']).date()).days > 90:
            raise ValueError('Stale holdings cannot be promoted')
        if not profile.get('country_weights') and not profile.get('sector_weights'):
            raise ValueError('No complete classification dimension to publish')
        validate_profile(profile)
        prepared.append((old_path,profile))
    # Validate every selected draft and index collision before changing anything.
    all_profiles = {p.stem:json.loads(p.read_text(encoding='utf-8')) for p in (published/'etfs').glob('*.json')}
    all_profiles.update({p.stem:profile for p,profile in prepared})
    index = {'schema_version':'1.0.0','generated_at':now(),
             'next_refresh_at':(datetime.now(timezone.utc)+timedelta(days=7)).replace(microsecond=0).isoformat().replace('+00:00','Z'),
             'counts':{'stocks':0,'etfs':len(all_profiles)},'symbols':{},'isins':{}}
    for key, profile in all_profiles.items():
        validate_profile(profile)
        path = f'etfs/{safe_key(key)}.json'
        for listing in profile['listings']:
            if listing['symbol'] in index['symbols']:
                raise ValueError('Listing collision')
            index['symbols'][listing['symbol']] = {'kind':'etf','path':path}
            if profile.get('isin'):
                index['symbols'][listing['symbol']]['isin'] = profile['isin']
        if profile.get('isin'):
            if profile['isin'] in index['isins']:
                raise ValueError('ISIN collision')
            index['isins'][profile['isin']] = path
    for path,profile in prepared:
        write_json(path,profile)
    write_json(published/'index.json',index)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run only after reviewing the exact selected drafts.')
    parser.add_argument('symbols',nargs='+')
    parser.add_argument('--review',type=Path,default=ROOT/'review')
    parser.add_argument('--published',type=Path,default=ROOT/'v1')
    args = parser.parse_args()
    promote(args.symbols,args.review,args.published)
