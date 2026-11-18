"""Bounded offline/native-local evidence checker; never connects, dispatches or admits."""
import argparse
import hashlib
import json
import math
import os
import re
import stat
import time

LIMIT = 65536
SOURCE_LIMIT = 4 * 1024 * 1024


def read_regular(path, limit):
    """Refuse symlinks in every component; bound reads and detect concurrent edits."""
    p = os.path.abspath(path)
    current = os.path.splitdrive(p)[0] + os.sep
    for component in p[len(current):].split(os.sep):
        current = os.path.join(current, component)
        info = os.lstat(current)
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise ValueError('linked-path')
    before = os.lstat(p)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError('unsafe-file')
    fd = os.open(p, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        start = os.fstat(fd)
        raw = b''
        while len(raw) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not chunk: break
            raw += chunk
        end = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if (not stat.S_ISREG(start.st_mode) or len(raw) > limit
                or identity(before) != identity(start) or identity(start) != identity(end)
                or identity(end) != identity(os.lstat(p))):
            raise ValueError('changed-file')
        return raw
    finally:
        os.close(fd)


def parse_receipt(raw):
    def unique(pairs):
        d = {}
        for k, v in pairs:
            if k in d: raise ValueError('duplicate-field')
            d[k] = v
        return d
    def invalid(_): raise ValueError('nonfinite-json')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def number(v):
    try: return type(v) in (int, float) and math.isfinite(v)
    except OverflowError: return False


def evaluate(receipt, *, expected_model, now, actual_digest=None, expected_digest=None):
    """All receipt values are untrusted claims. Local hash matching is not deployment proof."""
    result = {'kind': 'agiw.windows.offline-evidence.v1', 'admission': False,
              'nativeExecution': 'NOT_RUN', 'transportIdentity': 'UNVERIFIED',
              'workerDeployment': 'UNVERIFIED', 'observerOnly': False,
              'heartbeatClaim': 'INVALID', 'exactModelAdvertised': False,
              'workerVersionClaimPresent': False, 'localSourceDigest': 'NOT_CHECKED'}
    if expected_digest is not None:
        if not isinstance(expected_digest, str) or not re.fullmatch('[0-9a-f]{64}', expected_digest):
            result['localSourceDigest'] = 'INVALID_BASELINE'
        elif actual_digest is None:
            result['localSourceDigest'] = 'SOURCE_NOT_READ'
        else:
            result['localSourceDigest'] = 'MATCH' if actual_digest == expected_digest else 'MISMATCH'
    if not isinstance(receipt, dict): return result
    if receipt.get('scope') == 'local-observer' or 'workerVersion' in receipt:
        result['observerOnly'] = True
        result['heartbeatClaim'] = 'OBSERVER_NOT_WORKER_PROOF'
        return result
    if type(receipt.get('schema_version')) is not int or receipt['schema_version'] != 1:
        return result
    stamp = receipt.get('updated_unix')
    if not number(now) or not number(stamp): return result
    age = now - stamp
    if not number(age) or age < 0 or age > 30:
        result['heartbeatClaim'] = 'STALE_OR_FUTURE'
        return result
    if receipt.get('status') != 'running':
        result['heartbeatClaim'] = 'NOT_RUNNING'
        return result
    result['heartbeatClaim'] = 'FRESH_RUNNING_CLAIM_ONLY'
    models = receipt.get('models')
    result['exactModelAdvertised'] = (isinstance(expected_model, str) and bool(expected_model)
        and isinstance(models, list) and bool(models) and len(models) <= 64
        and all(isinstance(m, str) and 0 < len(m) <= 200 for m in models)
        and expected_model in models)
    version = receipt.get('worker_version')
    result['workerVersionClaimPresent'] = isinstance(version, str) and bool(re.fullmatch(r'[0-9A-Za-z._+-]{1,16}', version))
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--receipt', required=True, help='Explicit saved heartbeat file, never a live queue')
    p.add_argument('--expected-model', required=True)
    p.add_argument('--worker-source', help='Optional explicit local worker source file')
    p.add_argument('--expected-sha256', help='Independent trusted baseline digest, not a receipt value')
    args = p.parse_args(argv)
    try:
        receipt = parse_receipt(read_regular(args.receipt, LIMIT))
        actual = hashlib.sha256(read_regular(args.worker_source, SOURCE_LIMIT)).hexdigest() if args.worker_source else None
        report = evaluate(receipt, expected_model=args.expected_model, now=time.time(),
                          actual_digest=actual, expected_digest=args.expected_sha256)
        print(json.dumps(report, sort_keys=True))
        # Zero means a completed diagnostic, NEVER admission or native acceptance.
        return 0
    except (OSError, ValueError, TypeError):
        print(json.dumps({'kind':'agiw.windows.offline-evidence.v1', 'admission':False,
                          'status':'NOT_RUN', 'reason':'input-unreadable-unsafe-or-malformed'}))
        return 2

if __name__ == '__main__': raise SystemExit(main())
