"""Catalogue browse -> trusted receipt -> selected monitoring import, offline."""
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline.operations import Store
from workbench_operations_api import register_operations_routes


class CatalogWire:
    def __init__(self, shop, calls):
        self.shop, self.calls = shop, calls

    def post(self, path, body):
        self.calls.append((self.shop, path, body))
        if path == '/v3/product/list':
            start = 0 if not body['last_id'] else 2
            entries = [{'offer_id': f'offer-{i}', 'product_id': 100+i, 'sku': 900+i} for i in range(start, min(start+body['limit'], 3))]
            return {'result': {'items': entries, 'last_id': 'opaque-next' if start == 0 else '', 'total_items': 3}}
        if path == '/v3/product/info/list':
            return {'items': [{'id': int(value), 'offer_id': f'offer-{int(value)-100}', 'sku': 900+int(value)-100,
                               'name': f'Offline fixture {value}', 'currency_code': 'RUB', 'price': '100'} for value in body['product_id']]}
        raise AssertionError('No write endpoint is accepted')


class CatalogApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []
        self.app = FastAPI()
        self.shops = [{'id': 'a', 'enabled': True, 'credentials_ready': True},
                      {'id': 'b', 'enabled': True, 'credentials_ready': True},
                      {'id': 'disabled', 'enabled': False, 'credentials_ready': True},
                      {'id': 'missing', 'enabled': True, 'credentials_ready': False}]
        register_operations_routes(self.app, runtime_root=self.root,
                                   seller_transport=lambda shop: CatalogWire(shop, self.calls),
                                   shop_rows=lambda: self.shops,
                                   credential_context=lambda request: {'can_submit_credentials': True},
                                   require_credentials=lambda request: None, publication_rows=lambda: [], start_worker=False)
        self.client = TestClient(self.app, base_url='https://testserver')

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def read(self, shop='a', **fields):
        return self.client.post('/api/operations/catalog/read', json={'shop': shop, 'limit': 2, **fields})

    def add(self, token, **fields):
        return self.client.post('/api/operations/catalog/add', json={'shop': 'a', 'page_token': token,
                               'offer_ids': ['offer-0'], **fields})

    def test_explicit_read_safe_projection_and_trusted_next_page(self):
        self.client.get('/api/operations/config')
        self.assertEqual(self.calls, [])
        response = self.read()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        page = response.json()
        self.assertNotIn('next_cursor', page)
        self.assertNotIn('seen_cursors', page)
        self.assertEqual(page['total'], 3)
        following = self.read(previous_page_token=page['page_token']).json()
        self.assertEqual(following['items'][0]['offer_id'], 'offer-2')
        self.assertFalse(following['has_more'])
        self.assertEqual(self.calls[2][2]['last_id'], 'opaque-next')
        self.assertEqual(self.read(last_id='client-forged').status_code, 422)
        self.assertEqual(self.read(previous_page_token=following['page_token']).status_code, 409)

    def test_import_dedup_local_source_preservation_and_explicit_analysis(self):
        cache = Store(self.root / 'operations.sqlite3')
        cache.discover([{'shop': 'a', 'offer_id': 'offer-0', 'product_id': 'P000007', 'ozon_product_id': '100',
                         'source_note': 'Red / 24cm', 'source_url': 'https://detail.1688.com/offer/123.html'}])
        token = self.read().json()['page_token']
        result = self.add(token).json()
        self.assertEqual((result['existing'], result['imported'], result['jobs_enqueued']), (1, 0, 0))
        self.assertEqual(cache.product('a', 'offer-0')['source_note'], 'Red / 24cm')
        self.assertEqual(cache.jobs(), [])
        self.assertEqual(len(self.calls), 2)
        analyzed = self.add(token, analyze=True).json()
        self.assertEqual(analyzed['queued'], 1)
        self.assertEqual(analyzed['jobs_enqueued'], 1)
        duplicate = self.add(token, analyze=True).json()
        self.assertEqual((duplicate['queued'], duplicate['deduplicated']), (0, 1))
        self.assertEqual(len(cache.jobs()), 1)

    def test_scope_expiry_unknown_offer_and_raw_payload_are_rejected(self):
        token = self.read().json()['page_token']
        self.assertEqual(self.add(token, shop='b').status_code, 404)
        self.assertEqual(self.add(token, offer_ids=['forged-offer']).status_code, 422)
        self.assertEqual(self.add(token, rows=[{'ozon_product_id': '123'}]).status_code, 422)
        self.assertEqual(self.add(token, offer_ids=['offer-0']*101).status_code, 422)
        db = sqlite3.connect(self.root / 'operations.sqlite3')
        try:
            db.execute('UPDATE catalog_pages SET expires_at=0 WHERE token=?', (token,))
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.add(token).status_code, 409)
        self.assertEqual(Store(self.root / 'operations.sqlite3').list_products()['total'], 0)

    def test_disabled_missing_and_cross_site_do_not_contact_or_import(self):
        for shop, code in [('disabled', 409), ('missing', 409), ('unknown', 404)]:
            self.assertEqual(self.read(shop).status_code, code)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.post('/api/operations/catalog/read', json={'shop': 'a'},
                         headers={'origin': 'https://evil.invalid'}).status_code, 403)
        token = self.read().json()['page_token']
        self.assertEqual(self.client.post('/api/operations/catalog/add', json={'shop': 'a', 'page_token': token,
                         'offer_ids': ['offer-0']}, headers={'sec-fetch-site': 'cross-site'}).status_code, 403)

    def test_enqueue_storage_failure_reports_successful_import_separately(self):
        token = self.read().json()['page_token']
        with patch.object(Store, 'enqueue', side_effect=sqlite3.OperationalError('private-storage-message')):
            response = self.add(token, analyze=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['imported'], 1)
        self.assertEqual(response.json()['queued'], 0)
        self.assertEqual(response.json()['queue_errors'][0]['code'], 'queue_unavailable')
        self.assertNotIn('private-storage-message', response.text)
        self.assertEqual(Store(self.root / 'operations.sqlite3').list_products()['total'], 1)


if __name__ == '__main__':
    unittest.main()
