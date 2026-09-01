import os
import sys
import json
import unittest
from unittest.mock import patch, MagicMock

# Ensure parent directory is in sys.path
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

import server

class TestServerComprehensive(unittest.TestCase):

    def setUp(self):
        # Reset any global pending flow checks before each test
        server.PENDING_FLOW_CHECK = None

    def test_config_and_flows_loading(self):
        """Verify configuration and all 8 flows are loaded correctly."""
        general, aliases, servers = server.load_config()
        self.assertIn('030', servers)
        self.assertIn('036', servers)
        self.assertIn('027', servers)

        flows = server.get_flows()
        expected_flows = ['dtcc', 'bloomberg', 'swift', 'swing', 'position', 'nuvo', 'gcopy', 'notifications']
        for f_key in expected_flows:
            self.assertIn(f_key, flows, "Flow {0} must exist in flows.json".format(f_key))

    def test_status_parsing_edge_cases(self):
        """Test status parsing rules to prevent 'not running' matching 'running'."""
        cases = [
            ('Process not running', 'DOWN'),
            ('Repomessage is not running', 'DOWN'),
            ('JVM : Repobulkfile is not running', 'DOWN'),
            ('is running', 'UP'),
            ('PID file not found', 'DOWN'),
            ('Running', 'UP'),
            ('not running', 'DOWN'),
            ('stopped', 'DOWN'),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                res = server.parse_status(text)
                self.assertEqual(res, expected, "Failed parsing for text: '{0}'".format(text))

    def test_custom_status_script_parsing(self):
        """Test parsing of custom status script outputs (bancs_si_status.sh / bancs_batch_status.sh)."""
        # Mock _run_remote to simulate bancs_si_status.sh stdout
        si_up_output = (
            "--------------------------------------------\n"
            "       _ _    _      _______    _____ _____\n"
            "--------------------------------------------\n\n"
            "Running\n"
        )
        si_down_output = (
            "--------------------------------------------\n"
            "       _ _    _      _______    _____ _____\n"
            "--------------------------------------------\n\n"
            "not running\n"
        )

        with patch('server._run_remote') as mock_remote:
            # 1. Simulate SI UP
            mock_remote.return_value = (si_up_output, '', 0)
            res_up = server.svc_status('030', 'si')
            self.assertTrue(res_up['state'].startswith('UP'), "SI should be UP when status script returns Running")
            self.assertIn('Running', res_up['state'])

            # 2. Simulate SI DOWN
            mock_remote.return_value = (si_down_output, '', 0)
            res_down = server.svc_status('030', 'si')
            self.assertTrue(res_down['state'].startswith('DOWN'), "SI should be DOWN when status script returns not running")
            self.assertIn('not running', res_down['state'])

    def test_dtcc_flow_isolation(self):
        """
        Verify that DTCC flow check ONLY checks EAI and wmq-file-integrator,
        and DOES NOT check or fail on si or batch!
        """
        def mock_svc_status_side_effect(server_key, service):
            if service in ('EAI', 'wmq-file-integrator'):
                return {'state': 'UP', 'pid': '1234'}
            elif service in ('si', 'batch'):
                return {'state': 'DOWN (not running)', 'pid': '-'}
            return {'state': 'DOWN', 'pid': '-'}

        def mock_check_api_side_effect(api_cfg, secrets, ssl_ctx, server_cfg):
            return {
                'name': api_cfg.get('name'),
                'state': 'UP',
                'status_code': 200,
                'response_time_ms': 15,
                'team': '',
                'contact': ''
            }

        with patch('server.svc_status', side_effect=mock_svc_status_side_effect):
            with patch('api_checker.check_api', side_effect=mock_check_api_side_effect):
                with patch('server.VAULT_READY', True):
                    with patch('server.VAULT', MagicMock()):
                        resp = server._flow_check_response('dtcc', '030')
                        html = resp.get('html', '')
                        
                        # Verify DTCC checks EAI and wmq-file-integrator
                        self.assertIn('EAI', html)
                        self.assertIn('wmq-file-integrator', html)

                        # Verify DTCC DOES NOT check or include si or batch
                        self.assertNotIn('<td><b>si</b></td>', html)
                        self.assertNotIn('<td><b>batch</b></td>', html)

                        # Verify overall verdict is READY because EAI and wmq are UP
                        self.assertIn('READY to test', html)

    def test_intent_and_flow_chat_processing(self):
        """Test chat message processing for DTCC flow, help, and direct actions."""
        with patch('server._flow_check_response') as mock_flow_resp:
            mock_flow_resp.return_value = {'type': 'table', 'html': 'DTCC_MOCK_READY'}
            
            # Direct flow request with environment specified
            resp = server.process_message('check dtcc flow on uat', '030')
            self.assertEqual(resp.get('html'), 'DTCC_MOCK_READY')

        # Test prompt for environment if flow specified without env
        resp_prompt = server.process_message('dtcc flow', '030')
        self.assertIn('Which environment are you checking this flow on?', resp_prompt.get('html', ''))

        # Confirm environment multi-turn response
        resp_confirm = server.process_message('juat', '030')
        self.assertEqual(server.PENDING_FLOW_CHECK, None)

    def test_structured_server_logging(self):
        """Test production-grade log_event logging to activity.log and server.log."""
        act_log = os.path.join(PARENT_DIR, 'activity.log')
        srv_log = os.path.join(PARENT_DIR, 'server.log')

        # Clean existing files if present for clean verification
        for p in (act_log, srv_log):
            if os.path.exists(p):
                os.remove(p)

        server.log_event('INFO', 'TEST_MOD', 'Executing unit test log event')
        server.log_event('ERROR', 'VAULT', 'Simulated Vault failure', 'Connection timeout details...')

        # Verify activity.log
        self.assertTrue(os.path.exists(act_log))
        with open(act_log, 'r', encoding='utf-8') as f:
            content = f.read()
            self.assertIn('[INFO] [TEST_MOD] Executing unit test log event', content)
            self.assertIn('[ERROR] [VAULT] Simulated Vault failure', content)

        # Verify server.log
        self.assertTrue(os.path.exists(srv_log))
        with open(srv_log, 'r', encoding='utf-8') as f:
            content = f.read()
            self.assertIn('[INFO] [TEST_MOD] Executing unit test log event', content)
            self.assertIn('[ERROR] [VAULT] Simulated Vault failure', content)

    def test_http_endpoints(self):
        """Test HTTP server Handler GET and POST endpoints simulating browser requests."""
        from io import BytesIO

        class DummyWfile:
            def __init__(self):
                self.bytes = bytearray()
            def write(self, data):
                self.bytes.extend(data)
            def getvalue(self):
                return bytes(self.bytes)

        def make_handler(method, path, body_dict=None):
            handler = server.Handler.__new__(server.Handler)
            handler.command = method
            handler.path = path
            handler.headers = {'Content-Type': 'application/json'}
            handler.wfile = DummyWfile()
            handler.send_response = MagicMock()
            handler.send_header = MagicMock()
            handler.end_headers = MagicMock()
            handler.send_error = MagicMock()
            if body_dict is not None:
                body_bytes = json.dumps(body_dict).encode('utf-8')
                handler.headers['Content-Length'] = str(len(body_bytes))
                handler.rfile = BytesIO(body_bytes)
            return handler

        # 1. Test GET /api/envs
        h1 = make_handler('GET', '/api/envs')
        h1.do_GET()
        res1 = json.loads(h1.wfile.getvalue().decode('utf-8'))
        self.assertIn('envs', res1)
        self.assertEqual(len(res1['envs']), 3)

        # 2. Test GET /api/status?env=030
        with patch('server.svc_status', return_value={'state': 'UP', 'pid': '5555'}):
            h2 = make_handler('GET', '/api/status?env=030')
            h2.do_GET()
            res2 = json.loads(h2.wfile.getvalue().decode('utf-8'))
            self.assertEqual(res2['env'], '030')
            self.assertIn('services', res2)

        # 3. Test POST /api/chat
        with patch('server.process_message', return_value={'type': 'text', 'html': 'OK_CHAT_RESPONSE'}):
            h3 = make_handler('POST', '/api/chat', {'message': 'is EAI up?', 'env': '030'})
            h3.do_POST()
            res3 = json.loads(h3.wfile.getvalue().decode('utf-8'))
            self.assertEqual(res3.get('html'), 'OK_CHAT_RESPONSE')

if __name__ == '__main__':
    unittest.main()
