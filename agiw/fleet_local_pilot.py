"""Isolated M5 synthetic smoke: route policy -> Nisi -> loopback local-chat.
No automatic model loads, peer dispatch, hosted route or credential access.
"""
import argparse
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit
from .fleet_routing_plan import plan, WorkerEvidence, HostedEvidence
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

def route(evidence, source='agent', now=None):
    result=plan('local','routine',{'m5-macbook-pro':evidence},HostedEvidence(),now=time.time() if now is None else now,source=source)
    if result.worker != 'm5-macbook-pro' or result.preanalysis != 'local': raise Refused(result.reason)
    return asdict(result)

def clean_env():
    return {'PATH':'/usr/bin:/bin:/usr/local/bin','HOME':str(PILOT/'empty-home'),'TMPDIR':str(PILOT/'empty-home'),'PYTHONDONTWRITEBYTECODE':'1'}

def integrity():
    vendor=PILOT/'vendor/nisi-0.3.1'
    env=dict(clean_env(),GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null')
    def git(*a):return subprocess.check_output(['/usr/bin/git','-C',str(vendor),*a],env=env,text=True,timeout=5).strip()
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

def execute(mode, url, model, source='agent', program='nisi-smoke.mjs', report_store=None, node=None):
    if program not in PROGRAMS: raise Refused('PROGRAM_REFUSED')
    endpoint(url)
    if mode not in ('fixture','live'): raise Refused('MODE_REFUSED')
    integrity()
    store = report_store_dir(report_store)
    node_bin = node or node_binary()
    with (PILOT/'pilot.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise Refused('PREVIEW_ALREADY_RUNNING')
        ev = WorkerEvidence(time.time(),True,True,True,False) if mode=='fixture' else readiness(url,model)
        cfg={'mode':mode,'endpoint':url,'model':model,'route':route(ev,source),'reportStore':str(store)}
        # Node permission mode confines writes to the report store and reads to the
        # isolated preview plus that store. No provider env, HOME, child processes,
        # addons or worker permissions are supplied.
        cmd=[node_bin,'--permission','--allow-fs-read='+str(PILOT),
             '--allow-fs-read='+str(store),'--allow-fs-write='+str(store),str(PILOT/program)]
        r=subprocess.run(cmd,input=json.dumps(cfg),capture_output=True,text=True,env=clean_env(),cwd=PILOT,timeout=25)
        if not r.stdout: raise Refused('NISI_CHILD_FAILED: '+r.stderr[:300])
        data=json.loads(r.stdout)
        data['childExitCode'] = r.returncode
        data.update(pin=PIN,observedAt=time.time(),leaseScope='preview-only; not a backend reservation')
        report = data.get('report') if isinstance(data, dict) else {}
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
