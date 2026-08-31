import os
import sys
import unittest

# Ensure the parent directory is in the system path so we can import server and app
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

import server
import app

class TestUatOpsChatbot(unittest.TestCase):
    def test_parse_status_server(self):
        """Test parse_status in server.py with various inputs."""
        test_cases = [
            ('Process not running', 'DOWN'),
            ('Repomessage is not running', 'DOWN'),
            ('is running', 'UP'),
            ('PID file not found', 'DOWN'),
            ('Running', 'UP'),
            ('not running', 'DOWN'),
        ]
        for input_val, expected in test_cases:
            with self.subTest(input_val=input_val):
                self.assertEqual(server.parse_status(input_val), expected)

    def test_parse_status_app(self):
        """Test parse_status in app.py with various inputs."""
        test_cases = [
            ('Process not running', 'DOWN'),
            ('Repomessage is not running', 'DOWN'),
            ('is running', 'UP'),
            ('PID file not found', 'DOWN'),
            ('Running', 'UP'),
            ('not running', 'DOWN'),
        ]
        for input_val, expected in test_cases:
            with self.subTest(input_val=input_val):
                self.assertEqual(app.parse_status(input_val), expected)

    def test_wrap_env_cmd(self):
        """Test wrap_env_cmd prepends the environment profile sources and paths correctly."""
        cmd = 'test_command'
        expected_wrapped = (
            "source /etc/profile 2>/dev/null; "
            "source ~/.bash_profile 2>/dev/null; "
            "source ~/.bashrc 2>/dev/null; "
            "export PATH=$PATH:/usr/java/latest/bin:/usr/lib/jvm/java/bin:/usr/bin:/usr/local/bin; "
            "test_command"
        )
        self.assertEqual(server.wrap_env_cmd(cmd), expected_wrapped)
        self.assertEqual(app.wrap_env_cmd(cmd), expected_wrapped)

    def test_config_loader_server(self):
        """Test server.py load_config loads config properties properly."""
        general, aliases, servers = server.load_config()
        self.assertIn('default_user', general)
        self.assertEqual(general.get('default_user'), 'cpndev01')
        self.assertIn('eai', aliases)
        self.assertEqual(aliases.get('eai'), 'EAI')
        self.assertIn('030', servers)
        self.assertEqual(servers['030'].get('host'), 'cpnuatap030')

    def test_config_loader_app(self):
        """Test app.py load_config loads config properties properly."""
        general, aliases, servers = app.load_config()
        self.assertIn('default_user', general)
        self.assertEqual(general.get('default_user'), 'cpndev01')
        self.assertIn('eai', aliases)
        self.assertEqual(aliases.get('eai'), 'EAI')
        self.assertIn('030', servers)
        self.assertEqual(servers['030'].get('host'), 'cpnuatap030')

if __name__ == '__main__':
    unittest.main()
