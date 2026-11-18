"""Isolated M5 synthetic smoke: route policy -> Nisi -> loopback local-chat.
No automatic model loads, peer dispatch, hosted route or credential access.
"""
import argparse
from dataclasses import asdict
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit
from .fleet_routing_plan import plan, WorkerEvidence, HostedEvidence, WORKERS
try:  # POSIX lock; Windows has no fcntl and uses msvcrt.locking instead.
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None
ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / 'preview-pilot'
PIN = 'be7a5c99338947baf130bc0d0364eef12cbef5cb'
PROGRAMS = ('nisi-smoke.mjs', 'tiny-arithmetic.mjs', 'tiny-extraction.mjs')

class Refused(RuntimeError): pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args): raise Refused('REDIRECT_REFUSED')

def node_binary():
    chosen = os.environ.get('AGIW_NODE') or shutil.which('node')
    if not chosen: raise Refused('NODE_NOT_FOUND')
    return chosen

def endpoint(value):
    u = urlsplit(value)
    if u.scheme != 'http' or u.hostname != '127.0.0.1' or u.username or u.password or u.query or u.fragment or u.path != '/v1/chat/completions' or not u.port:
        raise Refused('LOOPBACK_ENDPOINT_REQUIRED')
    return 'http://127.0.0.1:%d' % u.port

def get(url):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with op.open(url, timeout=2) as r:
        if r.status != 200: raise Refused('HEALTH_HTTP')
        data = r.read(65537)
        if len(data)>65536: raise Refused('METADATA_CAP')
        return json.loads(data)

def _model_ids(items, loaded_only):
    ids = []
    for m in items:
        if not isinstance(m, dict): continue
        state = m.get('state')
        if loaded_only and state is not None and state != 'loaded': continue
        ident = m.get('id')
        if isinstance(ident, str) and ident and len(ident) <= 200: ids.append(ident)
    if len(ids) > 64: raise Refused('MODELS_CAP')
    return ids

def detect_models(base_url, getter=get):
    """llama-server /v1/models first; LM Studio /api/v0/models only on 404/unavailable."""
    try:
        data = getter(base_url + '/v1/models')
    except Exception:
        data, unavailable = None, True
    else:
        unavailable = False
    if not unavailable:
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            raise Refused('MODELS_INVALID')
        return _model_ids(data['data'], False), 'llama-server'
    try:
        data = getter(base_url + '/api/v0/models')
    except Exception:
        raise Refused('MODELS_UNAVAILABLE')
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        raise Refused('MODELS_INVALID')
    return _model_ids(data['data'], True), 'lm-studio'

def readiness(url, model, getter=get):
    base = endpoint(url)
    if getter(base+'/health').get('status') != 'ok': raise Refused('HEALTH_NOT_OK')
    models, source = detect_models(base, getter=getter)
    if model not in models: raise Refused('MODEL_NOT_VERIFIED')
    # llama.cpp /slots must explicitly establish every slot is idle. Missing is unknown.
    slots = getter(base+'/slots')
    if not isinstance(slots,list) or not slots or any(s.get('is_processing') is not False for s in slots):
        raise Refused('IDLE_NOT_VERIFIED')
    return WorkerEvidence(time.time(), True, True, True, False)

ROUTER_CONFIG = PILOT / 'fleet-workers.json'
# Per-lane model-call timeout (seconds). Monterey generates at ~1 tok/s, so it needs far longer.
DEFAULT_MODEL_TIMEOUT_S = 30
WORKER_MODEL_TIMEOUT_S = {'monterey-mac-pro': 400}
MAX_MODEL_TIMEOUT_S = 900

def model_timeout_s(worker_id, value=None):
    chosen = WORKER_MODEL_TIMEOUT_S.get(worker_id, DEFAULT_MODEL_TIMEOUT_S) if value is None else value
    if isinstance(chosen, bool) or not isinstance(chosen, (int, float)) or not 1 <= chosen <= MAX_MODEL_TIMEOUT_S:
        raise Refused('MODEL_TIMEOUT_INVALID')
    return int(chosen)

_EVIDENCE_KEYS = {'httpStatus': int, 'finishReason': str, 'errorName': str, 'errorCode': str, 'errorMessage': str,
                  'serverMessage': str, 'elapsedMs': (int, float), 'reportedModel': str, 'requestedModel': str,
                  'status': str, 'timeoutMs': int, 'disableThinking': bool, 'code': str}
_REDACT = [(re.compile(r'(bearer|token|api[_-]?key|authorization|password|secret)\s*[:=]?\s*\S+', re.I), r'\1 [redacted]'),
           (re.compile(r'[A-Za-z]:\\[^\s\'"]+'), '[path]'),
           (re.compile(r'(^|[\s\'"(=])/[^\s\'"]+'), r'\1[path]'),
           (re.compile(r'[A-Za-z0-9+/_=-]{24,}'), '[redacted]')]

def sanitize_text(value):
    if not isinstance(value, str) or not value: return None
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', value)
    for pattern, repl in _REDACT: text = pattern.sub(repl, text)
    return text.strip()[:200] or None

def author_evidence(receipts):
    """Whitelisted, sanitized per-call evidence for receipts: no payloads, tokens or paths."""
    out = []
    for item in (receipts if isinstance(receipts, list) else [])[:4]:
        if not isinstance(item, dict): continue
        row = {}
        for key, kind in _EVIDENCE_KEYS.items():
            value = item.get(key)
            if isinstance(value, kind) and not (kind is int and isinstance(value, bool)):
                row[key] = sanitize_text(value) if isinstance(value, str) else value
        out.append(row)
    return out

def known_workers(path=None):
    """Worker ids the router config knows. Must equal fleet_routing_plan.WORKERS."""
    config = json.loads(Path(path or ROUTER_CONFIG).read_text())
    workers = config.get('workers') if isinstance(config, dict) else None
    if (not isinstance(config, dict) or config.get('schemaVersion') != 1 or not isinstance(workers, list)
            or not workers or not all(isinstance(w, str) and w for w in workers) or len(set(workers)) != len(workers)):
        raise Refused('ROUTER_CONFIG_INVALID')
    if set(workers) != set(WORKERS):
        raise Refused('ROUTER_CONFIG_MISMATCH')
    return tuple(workers)

def check_worker_id(worker_id):
    if not isinstance(worker_id, str) or worker_id not in known_workers():
        raise Refused('UNKNOWN_WORKER_ID')
    return worker_id

def route(evidence, source='agent', now=None, worker_id='m5-macbook-pro'):
    check_worker_id(worker_id)
    result=plan('local','routine',{worker_id:evidence},HostedEvidence(),now=time.time() if now is None else now,source=source)
    if result.worker != worker_id or result.preanalysis != 'local': raise Refused(result.reason)
    return asdict(result)

def _is_windows():
    return os.name == 'nt'

def try_lock(handle):
    """Non-blocking exclusive preview lock. Fail closed when no lock primitive exists."""
    if fcntl is not None:
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError): raise Refused('PREVIEW_ALREADY_RUNNING')
        return 'fcntl'
    if msvcrt is not None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError: raise Refused('PREVIEW_ALREADY_RUNNING')
        return 'msvcrt'
    raise Refused('LOCK_UNAVAILABLE')

def git_binary():
    chosen = os.environ.get('AGIW_GIT') or shutil.which('git')
    if not chosen: raise Refused('GIT_NOT_FOUND')
    return chosen

def clean_env():
    home = str(PILOT / 'empty-home')
    env = {'HOME': home, 'TMPDIR': home, 'PYTHONDONTWRITEBYTECODE': '1'}
    if _is_windows():
        root = os.environ.get('SYSTEMROOT', r'C:\Windows')
        dirs = [os.path.join(root, 'System32'), root]
        for tool in (os.environ.get('AGIW_NODE') or shutil.which('node'), os.environ.get('AGIW_GIT') or shutil.which('git')):
            if tool: dirs.append(os.path.dirname(tool))
        env.update(PATH=os.pathsep.join(dirs), SYSTEMROOT=root, TEMP=home, TMP=home, USERPROFILE=home)
    else:
        env['PATH'] = '/usr/bin:/bin:/usr/local/bin'
    return env

def integrity():
    vendor=PILOT/'vendor/nisi-0.3.1'
    git_bin=git_binary()
    env=dict(clean_env(),GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull)
    def git(*a):return subprocess.check_output([git_bin,'-C',str(vendor),*a],env=env,text=True,timeout=5).strip()
    if git('rev-parse','HEAD') != PIN or git('status','--porcelain'):raise Refused('NISI_PIN_OR_CLEANLINESS')
    return PIN

def report_store_dir(path=None):
    directory = Path(path) if path else (PILOT / 'report-store')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory.resolve()

def _inside(directory, path):
    try:
        return os.path.commonpath([str(directory), str(Path(path).resolve())]) == str(directory)
    except (ValueError, OSError, TypeError):
        return False

def _verify_stored(store, data):
    report = data.get('report') if isinstance(data, dict) else None
    if not isinstance(report, dict): return 'REPORT_MISSING'
    if report.get('reportStored') is not True: return 'REPORT_NOT_STORED'
    path = data.get('reportStorePath')
    if not isinstance(path, str) or not path: return 'REPORT_PATH_MISSING'
    if not _inside(store, path) or not Path(path).is_file(): return 'REPORT_NOT_STORED'
    try:
        written = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return 'REPORT_UNREADABLE'
    if written.get('runId') != report.get('runId') or written.get('outcome') != report.get('outcome'):
        return 'REPORT_MISMATCH'
    return None

def execute(mode, url, model, source='agent', program='nisi-smoke.mjs', report_store=None, node=None, worker_id='m5-macbook-pro',
            timeout_s=None, disable_thinking=False):
    if program not in PROGRAMS: raise Refused('PROGRAM_REFUSED')
    check_worker_id(worker_id)
    timeout_s = model_timeout_s(worker_id, timeout_s)
    if disable_thinking not in (True, False): raise Refused('DISABLE_THINKING_INVALID')
    endpoint(url)
    if mode not in ('fixture','live'): raise Refused('MODE_REFUSED')
    integrity()
    store = report_store_dir(report_store)
    node_bin = node or node_binary()
    with (PILOT/'pilot.lock').open('a+') as lock:
        try_lock(lock)
        ev = WorkerEvidence(time.time(),True,True,True,False) if mode=='fixture' else readiness(url,model)
        cfg={'mode':mode,'endpoint':url,'model':model,'workerId':worker_id,'modelTimeoutMs':timeout_s*1000,'disableThinking':disable_thinking,'route':route(ev,source,worker_id=worker_id),'reportStore':str(store)}
        # Node permission mode confines writes to the report store and reads to the
        # isolated preview plus that store. No provider env, HOME, child processes,
        # addons or worker permissions are supplied.
        cmd=[node_bin,'--permission','--allow-fs-read='+str(PILOT),
             '--allow-fs-read='+str(store),'--allow-fs-write='+str(store),str(PILOT/program)]
        r=subprocess.run(cmd,input=json.dumps(cfg),capture_output=True,text=True,env=clean_env(),cwd=PILOT,timeout=timeout_s+30)
        if not r.stdout: raise Refused('NISI_CHILD_FAILED: '+(sanitize_text(r.stderr[:300]) or ''))
        data=json.loads(r.stdout)
        data['childExitCode'] = r.returncode
        data.update(pin=PIN,observedAt=time.time(),leaseScope='preview-only; not a backend reservation')
        report = data.get('report') if isinstance(data, dict) else {}
        data['authorEvidence'] = author_evidence(data.get('authorReceipts'))
        data['modelTimeoutS'] = timeout_s
        data['disableThinking'] = disable_thinking
        if r.returncode and r.stderr: data['childStderr'] = sanitize_text(r.stderr[:300])
        failure = _verify_stored(store, data)
        data['reportStored'] = report.get('reportStored') is True and failure is None
        data['reportPath'] = data.get('reportStorePath')
        data['runId'] = report.get('runId')
        data['reportSha256'] = report.get('storedReportSha256')
        if failure:
            report['outcome'] = 'BLOCKED'
            report['code'] = failure
        return data

def main():
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['fixture','live'],default='fixture');p.add_argument('--endpoint',default='http://127.0.0.1:8081/v1/chat/completions');p.add_argument('--model',default='fixture-only');p.add_argument('--source',default='agent');p.add_argument('--report-store',default=None);a=p.parse_args()
    try:
        if a.mode=='live' and a.model in ('fixture-only',''): raise Refused('MODEL_REQUIRED_IN_LIVE_MODE')
        data=execute(a.mode,a.endpoint,a.model,a.source,report_store=a.report_store)
        evidence=PILOT/'evidence';evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        out=evidence/('%s-%d.json'%(a.mode,time.time_ns()))
        out.write_text(json.dumps(data,indent=2)+'\n')
        report=data['report']
        print(json.dumps({'outcome':report['outcome'],'mode':a.mode,'modelCalls':data['modelCalls'],'independentLLMReview':False,'evidence':str(out),'reportStored':data['reportStored'],'reportPath':data['reportPath'],'runId':data['runId']}))
    except (Refused,OSError,ValueError,subprocess.SubprocessError) as e:
        print(json.dumps({'outcome':'BLOCKED','reason':str(e)}));return 2
    return 0 if data['report']['outcome'] == 'COMPLETED' else 1
if __name__=='__main__':raise SystemExit(main())
