import copy
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from build import build, classify, classify_fund
from common import basis_points, digest, safe_key, validate_profile, write_json
from promote import promote
from sources import invesco, metadata, moneydj_fund, parse_blackrock, parse_blackrock_xml, parse_date, vanguard
from validate import validate_tree

ENTRY = {'ticker':'TEST','symbol':'TEST','name':'Test ETF','issuer':'Test issuer',
         'listings':[{'symbol':'TEST','exchange_mic':'ARCX','currency':'USD'}],
         'sources':[{'adapter':'blackrock','url':'https://example.org/holdings'}]}
EVIDENCE = [{'source_url':'https://example.org/holdings','fetched_at':'2026-09-13T00:00:00Z','sha256':'test'}]


def raw():
    return {'as_of_date':date.today().isoformat(),'complete_holdings':True,'sector_taxonomy':'GICS',
            'country_basis':'issuer','denominator':'equity market value','issues':[],
            'holdings':[{'symbol':'A','name':'A','weight':.6,'value':60,'sector':'Information Technology','country':'US','equity':True},
                        {'symbol':'B','name':'B','weight':.4,'value':40,'sector':'Financials','country':'TW','equity':True}]}


class PipelineTests(unittest.TestCase):
    def test_partial_weights_are_not_scaled_to_full(self):
        self.assertEqual(basis_points({'US':.95}),{'US':9500})

    def test_rounding_is_deterministic(self):
        self.assertEqual(basis_points({'B':1/3,'C':1/3,'A':1/3},True),{'A':3334,'B':3333,'C':3333})

    def test_negative_and_nan_rejected(self):
        for value in [-.1,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):basis_points({'A':value})

    def test_path_traversal_and_windows_reserved_names(self):
        for key in ['../bad','CON.DE','a/b','NUL']:
            with self.assertRaises(ValueError):safe_key(key)

    def test_partial_sector_not_exported_as_complete(self):
        doc=raw();doc['holdings'][1]['sector']=None
        result=classify(ENTRY,doc,EVIDENCE)
        self.assertNotIn('sector_weights',result['profile'])
        self.assertEqual(result['metadata']['allocations']['sector_weights']['unallocated_basis_points'],4000)
        self.assertEqual(result['partial_allocations']['sector_weights'][0]['weight'],.6)
        self.assertIn('country_weights',result['profile'])

    def test_cash_is_not_equity_and_signed_cash_does_not_change_equity_denominator(self):
        doc=raw();doc['holdings'].append({'symbol':'USD','name':'cash','value':-1,'weight':-.01,'equity':False})
        result=classify(ENTRY,doc,EVIDENCE)
        self.assertEqual(result['profile']['sector_weights'][0]['weight'],.6)
        self.assertNotIn('asset_class_weights',result['profile'])

    def test_short_equity_is_rejected(self):
        doc=raw();doc['holdings'][0]['value']=-60
        with self.assertRaises(ValueError):classify(ENTRY,doc,EVIDENCE)

    def test_future_and_stale_dates(self):
        doc=raw();doc['as_of_date']=(date.today()+timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):classify(ENTRY,doc,EVIDENCE)
        doc['as_of_date']=(date.today()-timedelta(days=100)).isoformat()
        self.assertTrue(any('Stale' in r for r in classify(ENTRY,doc,EVIDENCE)['metadata']['reasons']))

    def test_issuer_dates(self):
        self.assertEqual(parse_date('2026年9月11日'),'2026-09-11')
        self.assertEqual(parse_date('10-Sep-2026'),'2026-09-10')

    def test_blackrock_percent_units_even_small_weights(self):
        text='Fund Holdings as of,"Sep 10, 2026"\n\nTicker,Name,Sector,Asset Class,Market Value,Weight (%),Location\nA,Name,Financials,Equity,100,0.5,United States\n'
        result=parse_blackrock(text.encode())
        self.assertEqual(result['holdings'][0]['weight'],.005)
        self.assertEqual(result['holdings'][0]['country'],'US')

    def test_blackrock_chinese_sector_alias_is_gics_materials(self):
        text='Fund Holdings as of,"Sep 10, 2026"\n\nTicker,Name,Sector,Asset Class,Market Value,Weight (%),Location\nBHP,BHP,原物料,指數股票型,100,0.5,澳洲\n'
        result=parse_blackrock(text.encode())
        self.assertEqual(result['holdings'][0]['sector'],'Materials')

    def test_blackrock_xml_nested_holdings_are_parseable(self):
        xml='''<?xml version="1.0"?><ss:Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><ss:Worksheet ss:Name="Holdings"><ss:Table><ss:Row><ss:Cell><ss:Data ss:Type="String">Issuer Ticker</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">Name</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">Sector</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">Asset Class</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">Market Value</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">Weight (%)</ss:Data></ss:Cell></ss:Row><ss:Row><ss:Cell><ss:Data ss:Type="String">ABC</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">ABC Corp</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">Financials</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">Equity</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">USD 100</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">10</ss:Data></ss:Cell></ss:Row><ss:Row><ss:Cell><ss:Data ss:Type="String">as of</ss:Data></ss:Cell><ss:Cell><ss:Data ss:Type="String">10/Sept/2026</ss:Data></ss:Cell></ss:Row></ss:Table></ss:Worksheet></ss:Workbook>'''
        result=parse_blackrock_xml(xml.encode())
        self.assertEqual(result['as_of_date'],'2026-09-10')
        self.assertEqual(result['holdings'][0]['country'],'IN')
        self.assertEqual(result['holdings'][0]['weight'],.1)

    def test_complete_candidate_is_ready_instead_of_review_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); published=root/'v1'; review=root/'review'
            class Fake:
                evidence=[]
                def get(self,*args): return b''
            source=copy.deepcopy(ENTRY['sources'][0]); source['adapter']='fixture'
            entry=copy.deepcopy(ENTRY); entry['sources']=[source]
            with patch('build.ADAPTERS', {'fixture':lambda e,s,f: (f.evidence.append(EVIDENCE[0]) or raw())}):
                result=build({'etfs':[entry]},review,published,Fake())
            self.assertEqual(result[0]['status'],'ready')
            self.assertNotIn('review_required', (review/'review.md').read_text(encoding='utf-8'))

    def test_metadata_fallback_is_not_source_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); published=root/'v1'; review=root/'review'
            class Fake:
                evidence=[]
                def get(self,*args):
                    self.evidence.append(EVIDENCE[0])
                    return b'<html>At closure 11 Sept 2026</html>'
            entry=copy.deepcopy(ENTRY)
            entry['sources']=[{'adapter':'metadata','url':'https://example.org/product','content_type':'html','asset_class':'Equity'}]
            with patch('build.ADAPTERS', {'metadata':metadata}):
                result=build({'etfs':[entry]},review,published,Fake())
            self.assertEqual(result[0]['status'],'metadata_only')
            self.assertNotIn('source_error', (review/'review.md').read_text(encoding='utf-8'))

    def test_error_does_not_delete_published_and_invalidates_stale_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); published=root/'v1'; review=root/'review'
            profile=classify(ENTRY,raw(),EVIDENCE)['profile']
            write_json(published/'etfs/TEST.json',profile)
            write_json(review/'drafts/TEST.json',{'status':'review_required'})
            class Fake:
                evidence=[]
                def get(self,*args):raise OSError('Source offline')
            result=build({'etfs':[ENTRY]},review,published,Fake())
            self.assertEqual(result[0]['status'],'source_error')
            self.assertEqual(json.loads((published/'etfs/TEST.json').read_text()),profile)
            self.assertIsNone(json.loads((review/'drafts/TEST.json').read_text())['profile'])

    def test_promote_and_validate_index_and_reject_stale_base(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); published=root/'v1'; review=root/'review'
            result=classify(ENTRY,raw(),EVIDENCE); profile=result['profile']
            draft={**result,'status':'ready','base_sha256':digest(None),'candidate_sha256':digest(profile)}
            write_json(review/'drafts/TEST.json',draft)
            promote(['TEST'],review,published)
            validate_tree(published)
            with self.assertRaises(ValueError):promote(['TEST'],review,published)

    def test_invalid_gics_and_dates_rejected_by_validator(self):
        profile=classify(ENTRY,raw(),EVIDENCE)['profile']
        profile['sector_weights'][0]['sector']='ICB Technology'
        with self.assertRaises(ValueError):validate_profile(profile)

    def test_direct_issuer_allocations_preserve_other_residuals(self):
        doc=raw()
        doc['direct_sectors']={'Information Technology':.8,'Other':.2}
        doc['direct_countries']={'US':.7,'Other':.3}
        result=classify(ENTRY,doc,EVIDENCE)
        self.assertEqual({r['sector'] for r in result['profile']['sector_weights']},{'Information Technology','Other'})
        other=next(r for r in result['profile']['country_weights'] if r['country']=='Other')
        self.assertIsNone(other['country_code'])

    def test_invesco_adapter_reads_same_date_aggregates(self):
        urls={'holdings':'https://example.org/holdings','sector':'https://example.org/sector','country':'https://example.org/country'}
        documents={
            urls['holdings']:{'isin':'IE000716YHJ7','effectiveDate':'2026-09-14',
                              'holdings':[{'name':'A','isin':'US0000000001','cusip':'000000001','weight':99.9},
                                          {'name':'Cash and/or Derivatives','weight':.1}]},
            urls['sector']:{'isin':'IE000716YHJ7','effectiveDate':'2026-09-14',
                            'holdingWeights':[{'name':'informationTechnology','value':90}, {'name':'other','value':10}]},
            urls['country']:{'isin':'IE000716YHJ7','effectiveDate':'2026-09-14',
                             'holdingWeights':[{'name':'UnitedStates','value':90}, {'name':'Other','value':10}]},
        }
        class Fake:
            def get(self,url,*args,**kwargs): return json.dumps(documents[url]).encode()
        result=invesco({'name':'FWRA','isin':'IE000716YHJ7'}, {'url':urls['holdings'],'sector_url':urls['sector'],'country_url':urls['country'],'isin':'IE000716YHJ7'}, Fake())
        self.assertEqual(result['as_of_date'],'2026-09-14')
        self.assertEqual(result['holdings'][0]['isin'],'US0000000001')
        self.assertEqual(result['direct_sectors']['Other'],.1)

    def test_vanguard_repeated_cursor_rejected(self):
        page={'data':{'funds':[{'profile':{'fundFullName':'Test'}}], 'borHoldings':[{'holdings':{'items':[], 'lastItemKey':'repeat','totalHoldings':2}}]}}
        class Fake:
            def get(self,*args):return json.dumps(page).encode()
        with self.assertRaisesRegex(ValueError,'Repeated'):
            vanguard(ENTRY,{'portfolio_id':'1','name_contains':'Test','url':'https://example.org'},Fake())

    def test_moneydj_fund_parses_asset_buckets_and_top_holdings(self):
        html = '''<html><body>Test Fund P
        <table><tr><td>基金投資分佈(依產業)</td></tr>
        <tr><th>產業</th><th>投資金額</th><th>比例(%)</th></tr>
        <tr><td>美國股票型</td><td>60</td><td>60</td></tr>
        <tr><td>債券型</td><td>30</td><td>30</td></tr>
        <tr><td>流動資金</td><td>10</td><td>10</td></tr>
        <tr><td>資料日期：2026/09/15</td></tr></table>
        <table><tr><th>投資名稱</th><th>投資</th><th>比例</th><th>增減</th></tr>
        <tr><td>Apple</td><td>1</td><td>6.5%</td><td>-</td></tr></table>
        </body></html>'''.encode()
        class Fake:
            def get(self, url): return html
        entry = {'name': 'Test Fund', 'isin': 'TW000T3628C8'}
        result = moneydj_fund(entry, {'url': 'https://example.org/fund', 'name_contains': 'Test Fund'}, Fake())
        self.assertEqual(result['as_of_date'], '2026-09-15')
        self.assertEqual(result['asset_class_weights']['Equity'], .6)
        self.assertEqual(result['asset_class_weights']['Fixed Income'], .3)
        self.assertEqual(result['top_holdings'][0]['name'], 'Apple')
        self.assertEqual(result['top_holdings'][0]['weight'], .065)

    def test_moneydj_nested_tables_count_allocations_and_holdings_once(self):
        html = (Path(__file__).parent / 'fixtures' / 'moneydj_nested_fund.html').read_bytes()
        class Fake:
            def get(self, url): return html
        result = moneydj_fund({'name': 'Test Fund'}, {'url': 'https://example.org/fund'}, Fake())
        self.assertEqual(result['as_of_date'], '2026-08-31')
        expected = {'Equity': .775, 'Fixed Income': .1549, 'Other': .0368, 'Cash': .0333}
        self.assertEqual(set(result['asset_class_weights']), set(expected))
        for key, weight in expected.items():
            self.assertAlmostEqual(result['asset_class_weights'][key], weight)
        self.assertAlmostEqual(sum(result['asset_class_weights'].values()), 1)
        names = [holding['name'] for holding in result['top_holdings']]
        self.assertEqual(len(names), 10)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[0], 'Vanguard標普500指數ETF')
        self.assertAlmostEqual(result['top_holdings'][0]['weight'], .1605)

    def test_fund_classifier_keeps_incomplete_lookthrough_explicit(self):
        raw_fund = {'as_of_date': date.today().isoformat(),
                    'asset_class_weights': {'Equity': .6, 'Fixed Income': .4},
                    'asset_class_basis': 'reported NAV', 'complete_holdings': False,
                    'top_holdings': [{'name': 'A', 'weight': .2}], 'issues': []}
        result = classify_fund({'symbol': 'FUND', 'name': 'Test Fund', 'issuer': 'Issuer',
                                'listings': [{'symbol': 'FUND', 'currency': 'TWD'}]},
                               raw_fund, EVIDENCE)
        self.assertEqual(result['profile']['kind'], 'fund')
        self.assertEqual(sum(row['weight'] for row in result['profile']['asset_class_weights']), 1)
        self.assertNotIn('sector_weights', result['profile'])
        self.assertIn('not a complete look-through', result['profile']['classification_notes'][0])

    def test_promote_fund_uses_funds_directory_without_gics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); published = root / 'v1'; review = root / 'review'
            entry = {'symbol': 'FUND', 'name': 'Test Fund', 'issuer': 'Issuer',
                     'listings': [{'symbol': 'FUND', 'currency': 'TWD'}]}
            result = classify_fund(entry, {'as_of_date': date.today().isoformat(),
                                           'asset_class_weights': {'Equity': .6, 'Cash': .4},
                                           'complete_holdings': False, 'issues': []}, EVIDENCE)
            draft = {**result, 'status': 'ready', 'base_sha256': digest(None),
                     'candidate_sha256': digest(result['profile'])}
            write_json(review / 'drafts/FUND.json', draft)
            promote(['FUND'], review, published)
            self.assertTrue((published / 'funds/FUND.json').is_file())
            validate_tree(published)


if __name__=='__main__':unittest.main()
