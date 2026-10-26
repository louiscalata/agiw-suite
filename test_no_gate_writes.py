"""The monitor cannot reach owner actions until L2 is explicitly opened."""

import ast
from contextlib import ExitStack
from email.message import Message
from functools import lru_cache
import io
import inspect
from itertools import product
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import online_code_repair as repair
import server


ROOT = Path(__file__).resolve().parent
def owner_actions():
    """Exercise every request shape accepted by the production dispatcher."""
    for path, (field, method, choices) in server.OWNER_ACTION_ROUTES.items():
        for value, action, arguments in choices:
            yield path, {field: value}, action, method, arguments


def owner_methods():
    tree = ast.parse((ROOT / 'online_code_repair.py').read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                 and node.name == 'OnlineCodeRepair')
    return {node.name: node for node in owner.body if isinstance(node, ast.FunctionDef)}


def first_statement(method):
    return next(node for node in method.body
                if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)))


def calls_named(method, name):
    return [node for node in ast.walk(method) if isinstance(node, ast.Call)
            and ast.unparse(node.func) == name]


def composed_strings(tree):
    """Fold source constants without importing or executing the source under test."""
    bindings = {}
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and node.value is not None:
                    bindings.setdefault(target.id, []).append(node.value)

    @lru_cache(maxsize=8192)
    def values(node, seen=frozenset()):
        if len(seen) > 16:
            return frozenset()
        result = set()
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            result.add(node.value)
        elif isinstance(node, ast.Name) and node.id not in seen:
            for expression in bindings.get(node.id, []):
                result.update(values(expression, seen | {node.id}))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            for left, right in product(values(node.left, seen), values(node.right, seen)):
                if type(left) is type(right):
                    result.add(left + right)
        elif isinstance(node, (ast.List, ast.Tuple, ast.JoinedStr)):
            elements = node.values if isinstance(node, ast.JoinedStr) else node.elts
            for parts in product(*(values(item.value if isinstance(item, ast.FormattedValue) else item, seen)
                                   for item in elements)):
                if isinstance(node, ast.JoinedStr):
                    if all(isinstance(item, str) for item in parts):
                        result.add(''.join(parts))
                else:
                    result.add(parts)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            for template in values(node.func.value, seen):
                if not isinstance(template, str):
                    continue
                for arguments in product(*(values(item, seen) for item in node.args)):
                    try:
                        if node.func.attr == 'join' and len(arguments) == 1:
                            result.add(template.join(arguments[0]))
                        elif node.func.attr == 'format' and not node.keywords:
                            result.add(template.format(*arguments))
                    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                        pass
        return frozenset(result)

    for node in nodes:
        for value in values(node):
            if isinstance(value, str):
                yield value


class StaticGuardTests(unittest.TestCase):
    def assert_owner_dispatch_is_guarded(self, source):
        tree = ast.parse(source)
        handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Handler')
        # All controller access in POST must be the deferred callback handed to
        # owner_action. A new direct route or captured bound method fails here.
        post = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == 'do_POST')
        guarded = calls_named(post, 'self.owner_action')
        self.assertEqual(len(guarded), 1)
        callback = guarded[0].args[1]
        self.assertIsInstance(callback, ast.Lambda)
        self.assertEqual(ast.unparse(callback.body),
                         'getattr(self.server.online_code_repair, method)(*arguments)')
        allowed = {id(node) for node in ast.walk(callback)}
        for node in ast.walk(post):
            if isinstance(node, ast.Attribute) and ast.unparse(node) == 'self.server.online_code_repair':
                # The only other access is the platform-availability None check.
                parent = next((parent for parent in ast.walk(post)
                               if node in list(ast.iter_child_nodes(parent))), None)
                none_check = (isinstance(parent, ast.Compare) and isinstance(parent.ops[0], ast.Is)
                              and len(parent.comparators) == 1
                              and isinstance(parent.comparators[0], ast.Constant)
                              and parent.comparators[0].value is None)
                self.assertTrue(id(node) in allowed or none_check,
                                f'owner access bypasses dispatch gate at line {node.lineno}')
        self.assertTrue(any(isinstance(node, ast.Name) and node.id == 'OWNER_ACTION_ROUTES'
                            for node in ast.walk(post)))

    def test_owner_http_dispatch_has_no_unregistered_bypass(self):
        self.assert_owner_dispatch_is_guarded((ROOT / 'server.py').read_text())
        requests = {name for name in owner_methods() if name == 'request' or name.startswith('request_')}
        self.assertEqual({method for _, _, _, method, _ in owner_actions()}, requests - {'request_entry'})
        self.assertEqual(set(server.OWNER_ACTION_ROUTES), {'/api/online-code-mode/repair',
                         '/api/inference/fix', '/api/online-code-mode/headless'})

    def test_dispatch_guard_rejects_a_future_unguarded_owner_route(self):
        source = (ROOT / 'server.py').read_text().replace(
            '    def do_POST(self):\n',
            "    def do_POST(self):\n        if self.path == '/api/new-owner':\n"
            "            return self.server.online_code_repair.request_entry()\n")
        with self.assertRaisesRegex(AssertionError, 'bypasses dispatch gate'):
            self.assert_owner_dispatch_is_guarded(source)

    def test_owner_command_sinks_require_a_leading_gate(self):
        # Sinks, not route/helper names: newly added paths reaching any owner
        # runner, mount, inference probe or journal must guard before any work.
        sinks = {'self._command_runner', '_run_bounded', 'self._inference_probe',
                 '_start_local_api', '_append_journal', 'share.mount', 'share.force_unmount'}
        found = set()
        for name, method in owner_methods().items():
            used = {ast.unparse(node.func) for node in ast.walk(method) if isinstance(node, ast.Call)} & sinks
            if used:
                with self.subTest(method=name, sinks=used):
                    self.assertEqual(ast.unparse(first_statement(method)), 'self._require_l2()')
                    found.update(used)
        self.assertEqual(found, sinks)

    def assert_safe_text(self, source, relative='fixture.sh'):
        # JS/Swift + and adjacent shell literals can split a forbidden path.
        folded = re.sub(r'''["']\s*(?:\+\s*)?["']''', '', source)
        for text in (source, folded):
            self.assertNotIn('gate_cli', text, str(relative))
            self.assertNotIn('agiw-mac-scheduler', text, str(relative))

    def assert_safe_python(self, source, relative='fixture.py'):
        tree = ast.parse(source, filename=str(relative))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [item.name for item in node.names]
                if isinstance(node, ast.ImportFrom):
                    names.append(node.module or '')
                self.assertFalse(any('gate_cli' in name for name in names), str(relative))
        for value in composed_strings(tree):
            self.assertNotIn('gate_cli', value, str(relative))
            self.assertNotIn('agiw-mac-scheduler', value, str(relative))
        self.assertNotIn('agiw-mac-scheduler', source, str(relative))

    def test_python_guard_rejects_composed_imports_and_scheduler_paths(self):
        sources = (
            '__import__("gate" + "_cli")',
            'import importlib as loader\nname = "gate"\nloader.import_module(f"{name}_cli")',
            'parts = ["gate", "cli"]\n__import__("_".join(parts))',
            'name = "gate"\n__import__("{}_cli".format(name))',
            'prefix = "agiw-"\npath = Path.home() / (prefix + "mac-scheduler")\npath.write_text("x")',
            'root = "agiw"\npath = Path(".local/state") / f"{root}-mac-scheduler"\npath.mkdir()',
            'path = os.path.join(".local/state", "-".join(["agiw", "mac", "scheduler"]))\nopen(path, "w")',
        )
        for source in sources:
            with self.subTest(source=source), self.assertRaises(AssertionError):
                self.assert_safe_python(source)

    def test_text_guard_rejects_composed_commands_and_scheduler_paths(self):
        sources = (
            ('fixture.js', 'const owner = "gate" + "_cli";'),
            ('fixture.mjs', 'const path = "agiw-" + "mac-" + "scheduler";'),
            ('fixture.swift', 'let path = "agiw-" + "mac-scheduler"'),
            ('fixture.sh', 'state="agiw-""mac-scheduler"'),
        )
        for relative, source in sources:
            with self.subTest(relative=relative), self.assertRaises(AssertionError):
                self.assert_safe_text(source, relative)

    def test_guards_accept_unrelated_composition(self):
        self.assert_safe_python('name = "peer"\n__import__(f"{name}_feed")\n'
                                'path = Path(".local/state") / "inference-monitor"\npath.mkdir()')
        self.assert_safe_text('state="inference-""monitor"')

    def test_production_modules_never_import_or_invoke_gate_cli(self):
        checked = 0
        for path in ROOT.rglob('*.py'):
            relative = path.relative_to(ROOT)
            if (path.name.startswith('test_') or 'tests' in relative.parts
                    or any(part.startswith('.') for part in relative.parts)):
                continue
            checked += 1
            self.assert_safe_python(path.read_text(), relative)
        self.assertGreater(checked, 20)

    def test_production_code_never_targets_the_scheduler_state_directory(self):
        for path in ROOT.rglob('*'):
            relative = path.relative_to(ROOT)
            if (path.suffix not in {'.py', '.swift', '.sh', '.js', '.mjs'}
                    or path.name.startswith('test_') or 'tests' in relative.parts
                    or any(part.startswith('.') for part in relative.parts)):
                continue
            self.assert_safe_text(path.read_text(), relative)


class ClosedL2Tests(unittest.TestCase):
    def test_entry_is_not_an_http_action_even_with_l2_open(self):
        for l2_open in (False, True):
            owner = mock.Mock()
            owner.request_entry.return_value = {'status': 'running'}
            owner.read.return_value = {'status': 'idle'}
            state = SimpleNamespace(l2_open=l2_open, online_code_repair=owner, action_diagnosis=None,
                                    server_address=('127.0.0.1', 8431), store=server.SnapshotStore())
            raw = b'{"action":"readiness"}'
            handler = server.Handler.__new__(server.Handler)
            handler.server, handler.path = state, '/api/online-code-mode/entry'
            handler.headers = Message()
            for key, value in {'Host': '127.0.0.1:8431', 'Origin': 'http://127.0.0.1:8431',
                               'Content-Type': 'application/json', 'Content-Length': str(len(raw))}.items():
                handler.headers[key] = value
            handler.rfile = io.BytesIO(raw)
            handler.reply = mock.Mock()
            with mock.patch.object(server, 'mac_online_code_controls_enabled', return_value=True):
                with self.subTest(l2_open=l2_open):
                    handler.do_POST()
                    self.assertEqual(handler.reply.call_args.args[0], 404)
            self.assertEqual(owner.mock_calls, [])
            # Existing UI polling reads shared action status through this GET.
            with mock.patch.object(server, 'mac_online_code_controls_enabled', return_value=True):
                handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0], 200)
            owner.read.assert_called_once_with()
            owner.request_entry.assert_not_called()

    def assert_diagnosis(self, result, action):
        self.assertEqual((result['status'], result['reasonCode'], result['action']),
                         ('needs-action', 'l2_closed', action))
        self.assertIs(result['readOnly'], True)
        self.assertTrue(result['diagnosis'])
        self.assertIn('--l2-open', result['nextStep'])
        self.assertIn(result['diagnosis'], result['message'])
        self.assertIn(result['nextStep'], result['message'])

    def test_direct_actions_default_to_diagnosis_without_worker_command_or_write(self):
        with ExitStack() as stack:
            blocked = [stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                       for target in ('threading.Thread', 'subprocess.Popen', 'os.open',
                                      'pathlib.Path.write_text', 'pathlib.Path.write_bytes',
                                      'online_code_repair._append_journal',
                                      'online_code_repair.launcher_runner')]
            controller = repair.OnlineCodeRepair()
            # Entry is internal-only but its receipt-writing command stays protected.
            for _, _, action, method, arguments in (*owner_actions(),
                                                    (None, None, 'readiness', 'request_entry', ())):
                with self.subTest(action=action):
                    result = getattr(controller, method)(*arguments)
                    self.assert_diagnosis(result, action)
                    self.assertEqual(result, controller.read())
                    self.assertIn('unknown', result['diagnosis'])
            self.assertTrue(controller.join(0))
            for blocked_call in blocked:
                blocked_call.assert_not_called()

    def test_diagnosis_projects_cached_machine_states_without_refreshing_them(self):
        snapshot = {'machines': [{'id': 'pc', 'host': 'pc', 'platform': 'windows',
                                 'state': 'unknown', 'reasonCode': 'stale', 'privateDetail': 'excluded'}]}
        read = mock.Mock(return_value=snapshot)
        controller = repair.OnlineCodeRepair(snapshot_read=read)
        result = controller.request()
        self.assert_diagnosis(result, 'check-and-repair')
        self.assertIn('windows: unknown (stale)', result['diagnosis'])
        self.assertEqual(result['machines'], [{'id': 'pc', 'host': 'pc', 'platform': 'windows',
                                              'state': 'unknown', 'reasonCode': 'stale'}])
        result['machines'][0]['state'] = 'up'
        self.assertEqual(controller.read()['machines'][0]['state'], 'unknown')
        self.assertEqual(snapshot['machines'][0]['state'], 'unknown')
        read.assert_called_once_with()

    def test_l2_flag_rejects_truthy_non_booleans_before_constructing_owners(self):
        for invalid in (None, 0, 1, '', 'false', 'true', [], {}):
            with self.subTest(flag=invalid):
                with self.assertRaisesRegex(ValueError, 'L2-open flag must be boolean'):
                    repair.OnlineCodeRepair(l2_open=invalid)
                with self.assertRaisesRegex(ValueError, 'L2-open flag must be boolean'):
                    server.MonitorServer(('127.0.0.1', 0), server.SnapshotStore(), l2_open=invalid)

    def test_direct_owner_helpers_cannot_bypass_closed_l2(self):
        controller = repair.OnlineCodeRepair()
        guarded = [name for name, method in owner_methods().items()
                   if calls_named(method, 'self._require_l2')]
        self.assertTrue(guarded)
        for name in guarded:
            operation = getattr(controller, name)
            arguments = {key: None for key, value in inspect.signature(operation).parameters.items()
                         if value.default is inspect.Parameter.empty}
            with self.subTest(operation=name):
                with self.assertRaises(repair._Stop) as stopped:
                    operation(**arguments)
                self.assertEqual(stopped.exception.status, 'needs-action')
                self.assertIn('L2 is closed', stopped.exception.message)
        controller._work(1, 'fix-all')
        self.assert_diagnosis(controller.read(), 'fix-all')

    def test_every_production_owner_command_is_blocked_at_both_dispatch_layers(self):
        commands = {}
        for method in owner_methods().values():
            for call in calls_named(method, 'self._call'):
                self.assertIsInstance(call.args[0], ast.Name,
                                      'new dynamic owner command needs an explicit test domain')
                name = call.args[0].id
                commands[name] = getattr(repair, name)
        self.assertTrue(commands)
        runner = mock.Mock(side_effect=AssertionError('owner runner invoked'))
        controller = repair.OnlineCodeRepair(runner=runner)
        for name, command in commands.items():
            for method, args in (('_call', (command,)), ('_runner', (command, None, 12))):
                with self.subTest(command=name, method=method), self.assertRaises(repair._Stop):
                    getattr(controller, method)(*args)
        runner.assert_not_called()

    def test_readiness_get_uses_only_cached_state_without_commands_threads_or_writes(self):
        state = SimpleNamespace(l2_open=False, online_code_repair=mock.Mock(),
                                server_address=('127.0.0.1', 8431), store=mock.Mock())
        state.store.read.return_value = {'sampledAt': 100, 'machines': [],
                                        'onlineCodeMode': {'setupState': 'invalid'}}
        handler = server.Handler.__new__(server.Handler)
        handler.server, handler.path = state, '/api/online-code-mode/readiness'
        handler.headers = Message()
        handler.headers['Host'] = '127.0.0.1:8431'
        handler.reply = mock.Mock()
        with ExitStack() as stack:
            blocked = [stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                       for target in ('threading.Thread', 'subprocess.Popen', 'os.open',
                                      'pathlib.Path.write_text', 'pathlib.Path.write_bytes',
                                      'online_code_repair._append_journal',
                                      'online_code_repair.launcher_runner')]
            stack.enter_context(mock.patch('server.time.time', return_value=100))
            handler.do_GET()
            for operation in blocked:
                operation.assert_not_called()
        status, raw, _ = handler.reply.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['reasonCode'], 'cached_observations')
        self.assertTrue(json.loads(raw)['readOnly'])
        self.assertEqual(json.loads(raw)['setupState'], 'invalid')
        state.store.read.assert_called_once_with()
        self.assertEqual(state.online_code_repair.mock_calls, [])

    def test_cli_enables_l2_only_with_the_explicit_flag(self):
        for arguments, expected in (([], {}), (['--l2-open'], {'l2_open': True}),
                                    (['--l2-open', '--durable-model-control'],
                                     {'l2_open': True, 'durable_model_control': True})):
            with self.subTest(arguments=arguments), \
                 mock.patch.object(sys, 'argv', ['server.py', *arguments]), \
                 mock.patch.object(server, 'MonitorServer', side_effect=RuntimeError('stop before bind')) as build:
                with self.assertRaisesRegex(RuntimeError, 'stop before bind'):
                    server.main()
                self.assertEqual(build.call_args.kwargs, expected)

    def test_server_default_and_explicit_flag_reach_the_controller(self):
        for options, expected in (({}, False), ({'l2_open': True}, True)):
            with self.subTest(options=options), \
                 mock.patch.object(server.ThreadingHTTPServer, '__init__', return_value=None), \
                 mock.patch.object(server, 'mac_online_code_controls_enabled', return_value=True), \
                 mock.patch.object(server, 'OnlineCodeRepair') as owner:
                store = server.SnapshotStore()
                instance = server.MonitorServer(('127.0.0.1', 0), store, **options)
                self.assertIs(instance.l2_open, expected)
                owner.assert_called_once_with(l2_open=expected, snapshot_read=store.read)

    def test_validated_routes_cannot_bypass_closed_l2_with_an_open_controller(self):
        owner = mock.Mock()
        state = SimpleNamespace(l2_open=False, online_code_repair=owner, action_diagnosis=None,
                                server_address=('127.0.0.1', 8431), store=server.SnapshotStore())
        with mock.patch.object(server, 'mac_online_code_controls_enabled', return_value=True):
            for path, body, action, method, _ in owner_actions():
                with self.subTest(action=action):
                    getattr(owner, method).return_value = {
                        'status': 'running', 'reasonCode': 'owner_invoked', 'action': action}
                    raw = json.dumps(body).encode()
                    handler = server.Handler.__new__(server.Handler)
                    handler.server, handler.path = state, path
                    handler.headers = Message()
                    for key, value in {'Host': '127.0.0.1:8431', 'Origin': 'http://127.0.0.1:8431',
                                       'Content-Type': 'application/json', 'Content-Length': str(len(raw))}.items():
                        handler.headers[key] = value
                    handler.rfile = io.BytesIO(raw)
                    handler.reply = mock.Mock()
                    handler.do_POST()
                    status, payload, _ = handler.reply.call_args.args
                    self.assertEqual(status, 423)
                    self.assert_diagnosis(json.loads(payload), action)
                    handler.do_GET()
                    status, payload, _ = handler.reply.call_args.args
                    self.assertEqual(status, 200)
                    self.assert_diagnosis(json.loads(payload), action)
        self.assertEqual(owner.mock_calls, [])

    def test_http_body_cannot_open_l2(self):
        owner = mock.Mock()
        state = SimpleNamespace(l2_open=False, online_code_repair=owner,
                                server_address=('127.0.0.1', 8431), store=server.SnapshotStore())
        with mock.patch.object(server, 'mac_online_code_controls_enabled', return_value=True):
            for path, body, action, _, _ in owner_actions():
                with self.subTest(action=action):
                    raw = json.dumps({**body, 'l2_open': True}).encode()
                    handler = server.Handler.__new__(server.Handler)
                    handler.server, handler.path = state, path
                    handler.headers = Message()
                    for key, value in {'Host': '127.0.0.1:8431', 'Origin': 'http://127.0.0.1:8431',
                                       'Content-Type': 'application/json', 'Content-Length': str(len(raw))}.items():
                        handler.headers[key] = value
                    handler.rfile = io.BytesIO(raw)
                    handler.reply = mock.Mock()
                    handler.do_POST()
                    self.assertEqual(handler.reply.call_args.args[0], 400)
        self.assertEqual(owner.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
