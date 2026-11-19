"""Decide which exception messages a golden comparison may pin.

The interpreter owns the wording of errors raised by built-in operations and
by the standard library. Only an explicit raise in the module under test is
stable enough to compare; everything else records the exception type alone.
"""

import copy
import dis

EXCEPTION_KEYS = ("error", "exception", "raised", "type")
UNPINNED_FIELDS = ("message", "args")


def _innermost(error):
    trace = error.__traceback__
    if trace is None:
        return None
    while trace.tb_next is not None:
        trace = trace.tb_next
    return trace


def explicit_raise(error, module):
    """True when the module under test raised this error with RAISE_VARARGS."""
    trace = _innermost(error)
    # Compare module globals by identity: no filesystem access, so I/O guards stay quiet.
    if trace is None or trace.tb_frame.f_globals is not vars(module):
        return False
    return any(
        item.offset == trace.tb_lasti and item.opname == "RAISE_VARARGS"
        for item in dis.get_instructions(trace.tb_frame.f_code)
    )


def pinned_message(error, module):
    """Return the message only when the module under test raised it explicitly."""
    return str(error) if explicit_raise(error, module) else None


def comparable(expected, actual):
    """Copy expected, dropping message or args that the harness did not pin.

    A field is dropped only where both sides carry the same exception-type
    value at the same path, so a changed type still fails the comparison.
    """
    result = copy.deepcopy(expected)
    _drop_unpinned(result, actual)
    return result


def _drop_unpinned(expected, actual):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in EXCEPTION_KEYS:
            if key in expected and key in actual and expected[key] == actual[key]:
                for field in UNPINNED_FIELDS:
                    if field in expected and field not in actual:
                        del expected[field]
        for key in list(expected):
            if key in actual:
                _drop_unpinned(expected[key], actual[key])
    elif isinstance(expected, list) and isinstance(actual, list):
        for left, right in zip(expected, actual):
            _drop_unpinned(left, right)
