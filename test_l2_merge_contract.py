"""A1 integration contract: run the real HTTP dispatcher without binding sockets."""
from email.message import Message
from io import BytesIO
import itertools
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from server import Handler

REASON = 'Repair, Fix and Headless require the default-off A1 L2 gate'
ROUTES = [('/api/online-code-mode/repair', {'action': 'check-and-repair'})]
ROUTES += [('/api/inference/fix', {'scope': scope}) for scope in ('local', 'route', 'both', 'nisi', 'all')]
ROUTES += [('/api/online-code-mode/headless', {'action': action}) for action in ('on', 'off')]


def dispatch(path, body, gate):
    handler = Handler.__new__(Handler)
    control = Mock()
    for name in ('request', 'request_fix', 'request_headless', 'request_entry', 'set_enabled'):
        getattr(control, name).return_value = {'status': 'running'}
    handler.server = SimpleNamespace(server_address=('127.0.0.1', 12345), online_code_repair=control,
                                     model_control=control, auto_unloader=control)
    if gate == 'none': handler.server.l2_gate = None
    raw = json.dumps(body).encode()
    handler.path, handler.rfile = path, BytesIO(raw)
    handler.headers = Message()
    for key, value in {'Host':'127.0.0.1:12345','Origin':'http://127.0.0.1:12345',
                       'Content-Type':'application/json','Content-Length':str(len(raw))}.items():
        handler.headers[key] = value
    handler.reply = Mock()
    with patch('server.mac_online_code_controls_enabled', return_value=True): handler.do_POST()
    return handler.reply.call_args.args[0], control


class L2MergeContract(unittest.TestCase):
    def test_model_control_remains_ungated(self):
        for gate, action in itertools.product(('missing','none'), ('load','unload')):
            with self.subTest(gate=gate,action=action):
                status, control = dispatch('/api/models/control',{'action':action,'modelId':'fixture'},gate)
                self.assertEqual(status,202)
                control.request.assert_called_once_with(action,'fixture')

    def test_auto_unload_remains_ungated(self):
        for gate, enabled in itertools.product(('missing','none'), (False,True)):
            with self.subTest(gate=gate,enabled=enabled):
                status, control = dispatch('/api/models/auto-unload',{'enabled':enabled},gate)
                self.assertEqual(status,200)
                control.set_enabled.assert_called_once_with(enabled)

    def test_receipt_writing_entry_is_not_exposed(self):
        for gate in ('missing','none'):
            with self.subTest(gate=gate):
                status, control = dispatch('/api/online-code-mode/entry',{'action':'readiness'},gate)
                self.assertEqual(status,404)
                control.request_entry.assert_not_called()


def gate_case(path, body, gate):
    def contract(self):
        status, control = dispatch(path, body, gate)
        self.assertIn(status, (403, 423, 503), f'{REASON}: {path} without gate returned {status}; calls={control.mock_calls}')
        self.assertEqual(control.mock_calls, [], REASON)
    contract.__doc__ = REASON
    return contract


for index, ((path,body),gate) in enumerate(itertools.product(ROUTES, ('missing','none'))):
    setattr(L2MergeContract, f'test_gate_required_{index:02d}_{path.rsplit("/",1)[1]}_{gate}',gate_case(path,body,gate))
