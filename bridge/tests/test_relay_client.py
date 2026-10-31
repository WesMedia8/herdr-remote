import json
from pathlib import Path
import tempfile
import unittest
from bridge.herdr_remote_bridge.herd_push import (RelayClient, RelayConfig, StatusWatcher, alert_payload,
                              content_state, register_bridge, handle_test_push)

class Response:
    status_code = 200
    def __init__(self, data): self.data = data
    def json(self): return self.data
    def raise_for_status(self): pass

class Transport:
    def __init__(self): self.calls = []
    def post(self, url, **kwargs):
        self.calls.append((url,kwargs))
        return Response(dict(bridge_id='id',bridge_secret='secret',pair_code='pair')
                        if url.endswith('/bridges') else dict(sent=2,failed=0,removed=0))
    def close(self): pass

class RelayClientTests(unittest.TestCase):
    def test_interface_and_registration(self):
        transport=Transport()
        calls=transport.calls
        with tempfile.TemporaryDirectory() as directory:
            config=register_bridge('https://relay.example','PC',directory,transport)
            self.assertEqual(RelayConfig.load(directory),config)
            self.assertEqual(Path(directory,'herd-relay.json').stat().st_mode & 0o777,0o600)
            relay=RelayClient(config,client=transport,machine='PC')
            self.assertTrue(relay.send_alert('ignored','production',alert_payload('Hi','Body','a'))['ok'])
            self.assertEqual(calls[-1][1]['json']['t'],'a')
            self.assertEqual(calls[-1][1]['headers']['Authorization'],'Bearer secret')
            relay.send_background(None,None)
            self.assertEqual(calls[-1][1]['json'],{'type':'background'})
            relay.send_live_activity(None,None,'update',content_state([]),1800000000)
            self.assertEqual(calls[-1][1]['json']['machine'],'PC')
            relay.send_live_activity(None,None,'start',content_state([]),1800000000,{'machine':'PC'})
            self.assertEqual(calls[-1][1]['json']['type'],'activity_start')
            self.assertEqual(handle_test_push({},relay,None)[1]['sent'],2)
            watcher=StatusWatcher(lambda:[],relay,None,'PC')
            watcher.poll([],0)
            jobs=watcher.poll([dict(address='a',status='blocked',agent='codex')],1)
            self.assertEqual(sum(j[0]=='update' for j in jobs),1)
            self.assertEqual(sum(j[0]=='start' for j in jobs),1)
            watcher._deliver(jobs)
            watcher.executor.shutdown()
    def test_failure_is_sanitized(self):
        class Broken:
            def post(self,*args,**kwargs): raise RuntimeError('secret')
        relay=RelayClient(RelayConfig('https://example','id','secret','pair'),client=Broken())
        result=relay.send_background(None,None)
        self.assertFalse(result['ok'])
        self.assertNotIn('secret',json.dumps(result))
