"""Offline HTTP safety tests: no sockets, account credentials or real cron."""
import io
import json
import unittest
from unittest.mock import patch

import bb_web as web


class SchedulerSafetyTests(unittest.TestCase):
    def handler(self, payload=None, headers=None):
        h = object.__new__(web.Handler)
        h.path = '/api/task/remove'
        h.client_address = ('127.0.0.1', 54321)
        raw = json.dumps(payload if payload is not None else {}).encode()
        csrf = web._new_csrf()
        h.headers = {'Host': 'bb.example', 'Origin': 'http://bb.example',
                     'Cookie': 'bb_csrf=' + csrf, 'X-CSRF-Token': csrf,
                     'Content-Type': 'application/json', 'Content-Length': str(len(raw))}
        h.headers.update(headers or {})
        h.rfile = io.BytesIO(raw)
        h.wfile = io.BytesIO()
        h.responses = []
        h._json = lambda obj, status=200: h.responses.append((status, obj))
        return h

    def call(self, h):
        with patch.object(web, 'TOKEN', ''), patch.object(web, 'TRUST_PROXY', False), \
                patch.object(web.core, 'remove_tasks', return_value=(True, 'removed')) as remove, \
                patch.object(web.core, 'log'):
            h.do_POST()
            return remove.call_count, h.responses[-1][0]

    def test_empty_confirmation_never_removes(self):
        self.assertEqual(self.call(self.handler()), (0, 400))

    def test_cross_site_never_removes(self):
        self.assertEqual(self.call(self.handler({'confirm': 'stop-schedule'},
                                               {'Origin': 'https://evil.example'})), (0, 403))

    def test_cross_site_fetch_metadata_rejected(self):
        self.assertEqual(self.call(self.handler({'confirm': 'stop-schedule'},
                                               {'Sec-Fetch-Site': 'cross-site'})), (0, 403))

    def test_form_rejected(self):
        self.assertEqual(self.call(self.handler({'confirm': 'stop-schedule'},
                                               {'Content-Type': 'text/plain'})), (0, 400))

    def test_missing_csrf_rejected(self):
        self.assertEqual(self.call(self.handler({'confirm': 'stop-schedule'},
                                               {'X-CSRF-Token': ''})), (0, 403))

    def test_unissued_csrf_rejected(self):
        self.assertEqual(self.call(self.handler({'confirm': 'stop-schedule'},
                                               {'Cookie': 'bb_csrf=forged',
                                                'X-CSRF-Token': 'forged'})), (0, 403))

    def test_valid_confirmed_request_removes(self):
        self.assertEqual(self.call(self.handler({'confirm': 'stop-schedule'})), (1, 200))

    def test_invalid_json_rejected(self):
        h = self.handler({'confirm': 'stop-schedule'})
        h.rfile = io.BytesIO(b'x' * int(h.headers['Content-Length']))
        self.assertEqual(self.call(h), (0, 400))

    def test_busy_operation_is_rejected(self):
        with web._task_lock:
            self.assertEqual(self.call(self.handler({'confirm': 'stop-schedule'})), (0, 409))

    def test_proxy_headers_only_trusted_when_enabled(self):
        h = self.handler(headers={'Origin': 'https://bb.example',
                                 'X-Forwarded-Proto': 'https'})
        with patch.object(web, 'TRUST_PROXY', False):
            self.assertFalse(h._write_allowed())
        with patch.object(web, 'TRUST_PROXY', True):
            self.assertTrue(h._write_allowed())
            h.client_address = ('203.0.113.9', 1)
            self.assertFalse(h._write_allowed())

    def test_frontend_has_confirmation_and_busy_guard(self):
        part = web.HTML.split('async function removeTask(){', 1)[1].split('async function loadLogs', 1)[0]
        self.assertIn('if(TASK_BUSY)return;', part)
        self.assertIn('if(!confirm(', part)
        self.assertIn("confirm:'stop-schedule'", part)
        self.assertIn('finally', part)


if __name__ == '__main__':
    unittest.main()
