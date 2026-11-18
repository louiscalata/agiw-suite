"""One owned ephemeral CPU-only smoke. Never touches existing servers."""
import sys,subprocess,socket,time,json,os,signal,datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from agiw.fleet_local_pilot import get,execute,clean_env
P=ROOT/'preview-pilot';E=P/'evidence';model=P/'tiny-model/SmolLM2-135M-Instruct-Q4_K_M.gguf'
exe='/Users/louiscalata/.lmstudio/extensions/backends/llama.cpp-mac-arm64-apple-metal-advsimd-2.73.0/llama-server'
assert subprocess.check_output(['/usr/sbin/sysctl','-n','kern.memorystatus_vm_pressure_level'],text=True).strip()=='1'
with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
cmd=['/usr/bin/nice','-n','15',exe,'-m',str(model),'--alias','agiw-tiny-smoke','--host','127.0.0.1','--port',str(port),'--device','none','-ngl','0','--no-op-offload','-t','1','-tb','1','--threads-http','1','-np','1','-c','1024','--slots','--offline','--no-warmup','--no-webui','--no-context-shift']
r={'startedUTC':datetime.datetime.now(datetime.timezone.utc).isoformat(),'command':cmd,'inferenceAttempted':False};proc=None
try:
 log=(E/'tiny-extraction-server.log').open('x');proc=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT,env=clean_env(),cwd=P,start_new_session=True);r['pid']=proc.pid
 deadline=time.monotonic()+25
 while time.monotonic()<deadline:
  if proc.poll() is not None:raise RuntimeError('SERVER_EXIT_'+str(proc.returncode))
  rss=int(subprocess.check_output(['/bin/ps','-o','rss=','-p',str(proc.pid)],text=True).strip());r['peakSampledRssKB']=max(r.get('peakSampledRssKB',0),rss)
  if rss>1024*1024:raise RuntimeError('MEMORY_CAP')
  try:
   if get('http://127.0.0.1:%d/health'%port).get('status')=='ok':break
  except Exception:pass
  time.sleep(.2)
 else:raise RuntimeError('STARTUP_TIMEOUT')
 r['health']=get('http://127.0.0.1:%d/health'%port);r['models']=get('http://127.0.0.1:%d/v1/models'%port);r['slotsBefore']=get('http://127.0.0.1:%d/slots'%port)
 with (E/'tiny-extraction-once.marker').open('x') as f:f.write(r['startedUTC'])
 r['inferenceAttempted']=True
 r['pilot']=execute('live','http://127.0.0.1:%d/v1/chat/completions'%port,'agiw-tiny-smoke',program='tiny-extraction.mjs')
except Exception as e:r['error']=type(e).__name__+': '+str(e)
finally:
 if proc is not None:
  if proc.poll() is None:
   proc.terminate()
   try:proc.wait(timeout=5)
   except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
  r['ownedProcessExitCode']=proc.returncode;r['ownedProcessReaped']=proc.poll() is not None
 r['finishedUTC']=datetime.datetime.now(datetime.timezone.utc).isoformat()
 (E/'tiny-extraction-result.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r,indent=2))
