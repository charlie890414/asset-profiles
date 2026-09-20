import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from sector_providers import parse_stockanalysis, parse_tradingview, provider_urls
from taiwan_lookup import lookup_missing

FIXTURES = Path(__file__).parent / 'fixtures'


class LiveShapeFetch:
    def __init__(self, sector='Non-Energy Minerals', industry='Other Metals/Minerals', stale=False,
                 wrong_isin=False, fail_sa=False, fail_tv=False):
        self.evidence, self.calls = [], []
        self.options = sector, industry, stale, wrong_isin, fail_sa, fail_tv

    def get(self, url):
        self.calls.append(url)
        tv_sector, tv_industry, stale, wrong_isin, fail_sa, fail_tv = self.options
        today = date.today()
        self.evidence.append({'source_url': url, 'sha256': 'fixture', 'fetched_at': today.isoformat()+'T00:00:00Z'})
        if 'openapi.twse' in url:
            stamp = f'{today.year-1911:03d}{today.month:02d}{today.day:02d}'
            return json.dumps([{'公司代號':'7610', '公司名稱':'聯友金屬', '產業別':'35', '出表日期':stamp}]).encode()
        if 'stockanalysis' in url:
            if fail_sa:
                raise OSError('Stock Analysis unavailable')
            content = (FIXTURES / 'stockanalysis-7610.html').read_text(encoding='utf-8')
            stamp = (today-timedelta(days=91) if stale else today).strftime('%b %d, %Y')
            return content.replace('Sep 13, 2026', stamp).encode()
        if fail_tv:
            raise OSError('TradingView unavailable')
        content = (FIXTURES / 'tradingview-7610.html').read_text(encoding='utf-8')
        return content.replace('Non-Energy Minerals', tv_sector).replace('Other Metals/Minerals', tv_industry).replace(
            'TW0007610B14', 'TW0002330008' if wrong_isin else 'TW0007610B14').encode()


def absent_vanguard(*args):
    return {'as_of_date': date.today().isoformat(), 'holdings': []}


class SectorProviderTests(unittest.TestCase):
    def lookup(self, fetch):
        holdings = [{'symbol':'7610', 'sector':None, 'equity':True, 'country':'TW'}]
        records, dates = lookup_missing(holdings, fetch, absent_vanguard)
        return holdings[0], records[0], dates

    def test_real_page_shapes_resolve_missing_sector(self):
        fetch = LiveShapeFetch()
        holding, record, dates = self.lookup(fetch)
        self.assertEqual(holding['sector'], 'Materials')
        self.assertEqual(record['status'], 'resolved_crosswalk')
        self.assertEqual(len(record['candidates']), 2)
        self.assertEqual(dates, [date.today().isoformat()])
        self.assertTrue(any('tradingview' in url for url in fetch.calls))
        self.assertIn('not issuer-native GICS', record['taxonomy'])

    def test_mismatched_isin_stale_or_failed_source_not_applied(self):
        for kwargs in [dict(wrong_isin=True), dict(stale=True), dict(fail_sa=True), dict(fail_tv=True)]:
            with self.subTest(kwargs=kwargs):
                fetch = LiveShapeFetch(**kwargs)
                holding, record, dates = self.lookup(fetch)
                self.assertIsNone(holding['sector'])
                self.assertEqual(record['status'], 'candidate')
                self.assertEqual(dates, [])
                self.assertTrue(any('tradingview' in url for url in fetch.calls))

    def test_conflicting_classifications_not_applied(self):
        holding, record, _ = self.lookup(LiveShapeFetch(sector='Health Technology', industry='Biotechnology'))
        self.assertIsNone(holding['sector'])
        self.assertEqual(record['status'], 'conflict')

    def test_unmapped_sector_remains_candidate(self):
        holding, record, _ = self.lookup(LiveShapeFetch(sector='Miscellaneous', industry='Unknown'))
        self.assertIsNone(holding['sector'])
        self.assertEqual(record['status'], 'candidate')

    def test_wrong_market_redirect_and_schema_changes_rejected(self):
        for name, url, parser in provider_urls('7610', 'TWSE'):
            content = (FIXTURES / ('stockanalysis-7610.html' if name == 'Stock Analysis' else 'tradingview-7610.html')).read_bytes()
            with self.subTest(name=name):
                parsed = parser(content, '7610', 'TWSE', url)
                self.assertEqual(parsed['sector'], 'Materials')
                with self.assertRaises(ValueError):
                    parser(content, '7611', 'TWSE', url)
                with self.assertRaises(ValueError):
                    parser(content, '7610', 'TPEx', url)
                with self.assertRaises(ValueError):
                    parser(content.replace(b'canonical', b'no-canonical'), '7610', 'TWSE', url)

    def test_tpex_urls_are_market_specific(self):
        urls = [u for _, u, _ in provider_urls('6173', 'TPEx')]
        self.assertIn('/quote/tpex/6173/', urls[0])
        self.assertIn('/symbols/TPEX-6173/', urls[1])


if __name__ == '__main__':
    unittest.main()
