"""Isolated M5 synthetic smoke: route policy -> Nisi -> loopback local-chat.
No automatic model loads, peer dispatch, hosted route or credential access.
"""
import argparse
from dataclasses import asdict
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit
from .fleet_routing_plan import plan, WorkerEvidence, HostedEvidence
ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / 'preview-pilot'
PIN = 'be7a5c99338947baf130bc0d0364eef12cbef5cb'
NODE = '/usr/local/bin/node'

class Refused(RuntimeError): pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args): raise Refused('REDIRECT_REFUSED')

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

def readiness(url, model, getter=get):
    base = endpoint(url)
    if getter(base+'/health').get('status') != 'ok': raise Refused('HEALTH_NOT_OK')
    models = getter(base+'/v1/models').get('data', [])
    if not any(m.get('id') == model for m in models): raise Refused('MODEL_NOT_VERIFIED')
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

def execute(mode, url, model, source='agent', program='nisi-smoke.mjs'):
    if program not in ('nisi-smoke.mjs', 'tiny-arithmetic.mjs', 'tiny-extraction.mjs'): raise Refused('PROGRAM_REFUSED')
    endpoint(url)
    if mode not in ('fixture','live'):raise Refused('MODE_REFUSED')
    integrity()
    with (PILOT/'pilot.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Refused('PREVIEW_ALREADY_RUNNING')
        ev=WorkerEvidence(time.time(),True,True,True,False) if mode=='fixture' else readiness(url,model)
        cfg={'mode':mode,'endpoint':url,'model':model,'route':route(ev,source)}
        # Node permission mode confines file reads to the isolated preview. No provider
        # env, user HOME, child processes, addons, or worker permissions are supplied.
        cmd=[NODE,'--permission','--allow-fs-read='+str(PILOT),str(PILOT/program)]
        r=subprocess.run(cmd,input=json.dumps(cfg),capture_output=True,text=True,env=clean_env(),cwd=PILOT,timeout=25)
        if not r.stdout:raise Refused('NISI_CHILD_FAILED: '+r.stderr[:300])
        data=json.loads(r.stdout)
        data['childExitCode'] = r.returncode
        data.update(pin=PIN,observedAt=time.time(),leaseScope='preview-only; not a backend reservation')
        return data

def main():
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['fixture','live'],default='fixture');p.add_argument('--endpoint',default='http://127.0.0.1:8081/v1/chat/completions');p.add_argument('--model',default='fixture-only');p.add_argument('--source',default='agent');a=p.parse_args()
    try:
        data=execute(a.mode,a.endpoint,a.model,a.source)
        out=PILOT/'evidence'/('%s-%d.json'%(a.mode,time.time_ns()))
        out.write_text(json.dumps(data,indent=2)+'\n')
        print(json.dumps({'outcome':data['report']['outcome'],'mode':a.mode,'modelCalls':data['modelCalls'],'independentLLMReview':False,'evidence':str(out)}))
    except (Refused,OSError,ValueError,subprocess.SubprocessError) as e:
        print(json.dumps({'outcome':'BLOCKED','reason':str(e)}));return 2
    return 0 if data['report']['outcome'] == 'COMPLETED' else 1
if __name__=='__main__':raise SystemExit(main())
