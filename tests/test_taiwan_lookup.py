import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from taiwan_lookup import EXCHANGES, exchange_records, lookup_missing


def holding(symbol='7610', sector=None, country='TW'):
    return {'symbol': symbol, 'sector': sector, 'country': country, 'equity': True}


class Fetch:
    def __init__(self, fail=False):
        self.evidence = []
        self.calls = []
        self.fail = fail

    def get(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError('source unavailable')
        self.evidence.append({'source_url': url, 'sha256': 'fixture'})
        return json.dumps([{'公司代號': '7610', '公司名稱': '聯友金屬科技股份有限公司',
                            '產業別': '35', '出表日期': '1150919'}]).encode()


def loader(holdings, as_of=None):
    def load(entry, source, fetch):
        return {'as_of_date': as_of or date.today().isoformat(), 'holdings': holdings}
    return load


class TaiwanLookupTests(unittest.TestCase):
    def test_missing_only_and_exact_taiwan_match(self):
        holdings = [holding(), holding('2330', 'Information Technology'), holding(country='US')]
        fetch = Fetch()
        records, dates = lookup_missing(holdings, fetch, loader([
            holding(sector='Materials'), holding(sector='Industrials', country='US')]))
        self.assertEqual(holdings[0]['sector'], 'Materials')
        self.assertEqual(holdings[1]['sector'], 'Information Technology')
        self.assertIsNone(holdings[2]['sector'])
        self.assertEqual(records[0]['status'], 'resolved')
        self.assertEqual(dates, [date.today().isoformat()])
        self.assertEqual(fetch.calls, [])

    def test_no_missing_means_no_requests(self):
        def unexpected(*args):
            self.fail('unnecessary lookup')
        self.assertEqual(lookup_missing([holding(sector='Materials')], Fetch(), unexpected), ([], []))

    def test_conflicts_are_not_applied_and_exchange_is_not_gics(self):
        holdings = [holding()]
        records, dates = lookup_missing(holdings, Fetch(), loader([
            holding(sector='Materials'), holding(sector='Industrials')]))
        self.assertIsNone(holdings[0]['sector'])
        self.assertEqual(records[0]['status'], 'conflict')
        self.assertEqual(records[0]['attempts'][1]['company']['industry_code'], '35')
        self.assertEqual(dates, [])

    def test_absent_gics_has_exchange_evidence_without_assignment(self):
        holdings = [holding()]
        records, _ = lookup_missing(holdings, Fetch(), loader([]))
        self.assertIsNone(holdings[0]['sector'])
        self.assertEqual(records[0]['status'], 'unresolved')
        self.assertEqual(records[0]['attempts'][1]['company']['as_of_date'], '2026-09-19')
        self.assertEqual(records[0]['attempts'][1]['evidence'][0]['sha256'], 'fixture')

    def test_stale_future_and_network_errors_leave_unclassified(self):
        for delta in [-91, 1]:
            with self.subTest(delta=delta):
                holdings = [holding()]
                records, dates = lookup_missing(holdings, Fetch(fail=True), loader(
                    [holding(sector='Materials')], (date.today() + timedelta(days=delta)).isoformat()))
                self.assertIsNone(holdings[0]['sector'])
                self.assertEqual(dates, [])
                self.assertEqual(len(records[0]['attempts']), 7)
                self.assertTrue(all('error' in a for a in records[0]['attempts']))

    def test_tpex_schema_and_changed_schema(self):
        row = {'SecuritiesCompanyCode': '1234', 'CompanyName': 'Test',
               'SecuritiesIndustryCode': '24', 'Date': '1150920'}
        result = exchange_records(json.dumps([row]), EXCHANGES[1][2])
        self.assertEqual(result['1234']['industry_code'], '24')
        with self.assertRaisesRegex(ValueError, 'schema changed'):
            exchange_records('[{"unexpected": 1}]', EXCHANGES[0][2])


if __name__ == '__main__':
    unittest.main()
