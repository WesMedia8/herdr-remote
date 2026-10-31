import plistlib
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from bridge.herdr_remote_bridge import server, service


class DetectionTests(unittest.TestCase):
    def test_interface_samples(self):
        samples = {
            'ifconfig': 'en0: inet 192.168.1.4\nutun4: flags=8051\n    inet 100.101.102.103 --> 100.101.102.103 netmask 0xffffffff\n inet 100.90.0.2',
            'ip': '1: lo inet 127.0.0.1/8 scope host lo\n4: tailscale0 inet 100.101.102.103/32 scope global tailscale0',
            'ipconfig': 'Ethernet adapter:\n IPv4 Address. . . . . . . . . . . : 192.168.1.2\n IPv4 Address. . . . . . . . . . . : 100.101.102.103(Preferred)',
        }
        for method, sample in samples.items():
            with self.subTest(method=method):
                self.assertEqual(server.parse_interface_ipv4(sample, method), '100.101.102.103')
        for value in ['100.63.255.255', '100.128.0.0', '999.64.0.1', '192.168.0.1']:
            self.assertIsNone(server.parse_interface_ipv4(value, 'cli'))
        for value in ['100.64.0.0', '100.127.255.255']:
            self.assertEqual(server.parse_interface_ipv4(value, 'cli'), value)

    def test_detection_order_and_fallback(self):
        with patch.object(server.platform, 'system', return_value='Darwin'), patch.object(server.shutil, 'which', return_value='/bin/tailscale'), patch.object(server.subprocess, 'run') as run:
            run.side_effect = [OSError(), SimpleNamespace(returncode=1, stdout=''), SimpleNamespace(returncode=0, stdout='utun4 inet 100.101.102.103 --> 100.101.102.103')]
            self.assertEqual(server.detect_tailscale(), ('100.101.102.103', 'ifconfig'))
            self.assertEqual([c.args[0] for c in run.call_args_list], [['/bin/tailscale', 'ip', '-4'], ['/Applications/Tailscale.app/Contents/MacOS/Tailscale', 'ip', '-4'], ['/sbin/ifconfig']])


class ServiceTextTests(unittest.TestCase):
    def test_plist(self):
        data = plistlib.loads(service.render_plist('/a & b/bridge', '/config', '/bin', '/home/user').encode())
        self.assertEqual(data['ProgramArguments'], ['/a & b/bridge', 'serve'])
        self.assertTrue(data['KeepAlive'])
        self.assertTrue(data['RunAtLoad'])
        self.assertEqual(data['EnvironmentVariables']['HERD_CONFIG'], '/config')
        self.assertEqual(data['StandardOutPath'], '/home/user/Library/Logs/herdr-remote-bridge.log')

    def test_unit(self):
        text = service.render_unit('/a b/bridge', '/c%/config', '/bin')
        self.assertIn('ExecStart="/a b/bridge" serve', text)
        self.assertIn('HERD_CONFIG=/c%%/config', text)
        self.assertIn('WantedBy=default.target', text)

    def test_task(self):
        root = ET.fromstring(service.render_task('C:\\a & b\\pythonw.exe', '"C:\\runner.py"', 'C:\\', 'PC\\User'))
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(root.find('.//t:Command', ns).text, 'C:\\a & b\\pythonw.exe')
        self.assertEqual(root.find('.//t:Hidden', ns).text, 'true')
        self.assertEqual(root.find('.//t:LogonType', ns).text, 'InteractiveToken')
        self.assertIsNotNone(root.find('.//t:LogonTrigger', ns))

class AdditionalDetectionTests(unittest.TestCase):
    def test_platform_fallbacks_and_failure(self):
        for system, method, output, expected_command in [
            ('Windows', 'ipconfig', 'IPv4 Address. . . : 100.64.0.2', ['ipconfig']),
            ('Linux', 'ip', '4: tailscale0 inet 100.64.0.2/32', ['ip', '-4', '-o', 'addr']),
        ]:
            with self.subTest(system=system), patch.object(server.platform, 'system', return_value=system), patch.object(server.shutil, 'which', return_value=None), patch.object(server.subprocess, 'run') as run:
                run.side_effect = lambda args, **kw: SimpleNamespace(returncode=0, stdout=output if args == expected_command else '')
                self.assertEqual(server.detect_tailscale(), ('100.64.0.2', method))
                self.assertEqual(run.call_args.args[0], expected_command)
                run.side_effect = OSError()
                self.assertEqual(server.detect_tailscale(), (None, None))

    def test_path_wins(self):
        with patch.object(server.shutil, 'which', return_value='/custom/tailscale'), patch.object(server.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='100.70.0.1\n')) as run:
            self.assertEqual(server.tailscale_ip(), '100.70.0.1')
            run.assert_called_once()
