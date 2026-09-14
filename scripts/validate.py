import json
import sys
from pathlib import Path
import jsonschema
from common import ROOT, validate_profile


def validate_tree(root):
    index = json.loads((root/'index.json').read_text(encoding='utf-8'))
    schema = json.loads((ROOT/'schema/index.schema.json').read_text(encoding='utf-8'))
    jsonschema.Draft202012Validator(schema,format_checker=jsonschema.FormatChecker()).validate(index)
    profiles = list((root/'etfs').glob('*.json'))
    if index['counts'] != {'stocks':0,'etfs':len(profiles)}:
        raise ValueError('Index counts mismatch')
    expected = {}
    for path in profiles:
        profile = json.loads(path.read_text(encoding='utf-8'))
        validate_profile(profile)
        for listing in profile['listings']:
            symbol = listing['symbol']
            if symbol in expected:
                raise ValueError('Listing collision')
            expected[symbol] = 'etfs/'+path.name
    if expected != {s:v['path'] for s,v in index['symbols'].items()}:
        raise ValueError('Index mappings mismatch')
    for value in index['isins'].values():
        if not (root/value).is_file() or not (root/value).resolve().is_relative_to(root.resolve()):
            raise ValueError('Invalid ISIN path')


if __name__ == '__main__':
    validate_tree(Path(sys.argv[1] if len(sys.argv)>1 else ROOT/'v1'))
    print('Dataset valid')
