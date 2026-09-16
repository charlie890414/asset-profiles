import json
import sys
from pathlib import Path
import jsonschema
from common import ROOT, validate_profile


def validate_tree(root):
    index = json.loads((root/'index.json').read_text(encoding='utf-8'))
    schema = json.loads((ROOT/'schema/index.schema.json').read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator(schema,format_checker=jsonschema.FormatChecker()).validate(index)
    profile_paths = {
        'etfs': list((root/'etfs').glob('*.json')),
        'funds': list((root/'funds').glob('*.json')),
    }
    profiles = [path for paths in profile_paths.values() for path in paths]
    expected_counts = {'stocks': 0, 'etfs': len(profile_paths['etfs']),
                       'funds': len(profile_paths['funds'])}
    if index['counts'] != expected_counts:
        raise ValueError('Index counts mismatch')
    expected = {}
    for path in profiles:
        profile = json.loads(path.read_text(encoding='utf-8'))
        validate_profile(profile)
        directory = path.parent.name
        expected_kind = 'fund' if directory == 'funds' else 'etf'
        if profile.get('kind') != expected_kind:
            raise ValueError(f'{path.name}: profile kind does not match directory')
        for listing in profile['listings']:
            symbol = listing['symbol']
            if symbol in expected:
                raise ValueError('Listing collision')
            expected[symbol] = directory+'/'+path.name
    if expected != {s:v['path'] for s,v in index['symbols'].items()}:
        raise ValueError('Index mappings mismatch')
    for symbol, value in index['symbols'].items():
        expected_kind = 'fund' if value['path'].startswith('funds/') else 'etf'
        if value['kind'] != expected_kind:
            raise ValueError(f'{symbol}: index kind/path mismatch')
    for value in index['isins'].values():
        if not (root/value).is_file() or not (root/value).resolve().is_relative_to(root.resolve()):
            raise ValueError('Invalid ISIN path')


if __name__ == '__main__':
    validate_tree(Path(sys.argv[1] if len(sys.argv)>1 else ROOT/'v1'))
    print('Dataset valid')
