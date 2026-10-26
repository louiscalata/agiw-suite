"""Two-sided publication fixtures. I/O is private files or mocked adapters only."""
import copy
import errno
import itertools
import importlib.util
import os
import json
import tempfile
from pathlib import Path
from unittest import mock

import telemetry
from windows.agiw_win import probes
from test_support.pipeline_dimensions import record

if os.environ.get('A4_RENDER_ROOT'):
    spec = importlib.util.spec_from_file_location('baseline_probes', Path(os.environ['A4_RENDER_ROOT'])/'windows/agiw_win/probes.py')
    probes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probes)
    spec = importlib.util.spec_from_file_location('baseline_telemetry', Path(os.environ['A4_RENDER_ROOT'])/'telemetry.py')
    worker_telemetry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker_telemetry)
else:
    worker_telemetry = telemetry


def dimensions(base, now):
    def item(dimension, parameters, property='no-false-doubt'):
        return {'label': dimension + ':' + json.dumps(parameters, sort_keys=True),
                'dimension': dimension, 'property': property, 'input': parameters,
                'changes': {}, 'snapshot': copy.deepcopy(base), 'errors': [], 'expect': {}}

    def jobs(case, results=2, opens=0, event_age=10, observed_age=0):
        rows = [record(now, i+1, offset=-event_age, lane=['amd','bionic'][i % 2],
                       client='codex', predictedPerSecond=40+i, promptPerSecond=100+i)
                for i in range(results)]
        rows += [record(now, i+100, 'enqueued', offset=-1, lane='amd', client='codex') for i in range(opens)]
        raw = b''.join(json.dumps(row).encode()+b'\n' for row in rows)
        with mock.patch.object(telemetry, '_read_windows_journal', return_value=(raw, False)), mock.patch.object(telemetry, '_pid_alive', return_value=True):
            block, status = telemetry._windows_jobs(now)
        status['observedAgeSeconds'] = observed_age
        d=case['snapshot']; d['windowsJobs']=block
        d['sources']=[s for s in d['sources'] if s['id']!='windows-jobs']+[status]
        return block

    # Empty initialized journals have no checkpoint clock. Positive primary
    # routes survive a display cap; only the aggregate loses exactness.
    for age, complete, checkpoint in itertools.product((None, -.001, 0, 29.999, 30, 30.001), (True, False), (None, 0, 60)):
        case=item('idle-scan-clock',dict(age=age,complete=complete,checkpoint=checkpoint))
        d=case['snapshot']
        d['pipeline']=dict(status='idle',runId=None,ageSeconds=checkpoint,pipelines=[],pipelinesComplete=complete)
        d['onlineCodeMode'].update(state='ready',active=False,taskState='idle')
        next(s for s in d['sources'] if s['id']=='pipeline-router')['observationAgeSeconds']=age
        case['expect']['idleScan']=age is not None and 0 <= age <= 30 and complete
        case['property']='no-false-doubt' if case['expect']['idleScan'] else 'no-false-certainty'
        yield case

    for state, count, age, checkpoint in itertools.product(('running','queued'), (1,8,9), (0,30,30.001), (None,60)):
        case=item('primary-route-cap',dict(state=state,count=count,age=age,checkpoint=checkpoint))
        d=case['snapshot']; cut=count>8
        d['pipeline']=dict(status=state,runId='route',ageSeconds=checkpoint,pipelinesComplete=not cut,runsTruncated=cut,
            pipelines=[dict(runId='route' if i==0 else f'route-{i}',status=state,live=True,ageSeconds=checkpoint) for i in range(min(count,8))])
        d['onlineCodeMode'].update(state='processing',active=True,taskState=state,runCountsComplete=not cut)
        next(s for s in d['sources'] if s['id']=='pipeline-router')['observationAgeSeconds']=age
        case['expect'].update(primaryState=state if age<=30 else 'unknown',primaryComplete=not cut,primaryCurrent=age<=30)
        case['property']='no-false-doubt' if age<=30 else 'no-false-certainty'
        yield case

    for shape, count, age in itertools.product(('missing','null','negative','boolean','float','mismatch','exact'), (0,1,2), (0,30,30.001)):
        case=item('waiter-count-shape',dict(shape=shape,count=count,age=age))
        d=case['snapshot']; b=jobs(case,results=0,opens=count,observed_age=age)
        values=dict(null=None,negative=-1,boolean=True,float=count+.5,mismatch=count+1,exact=count)
        if shape=='missing': b.pop('unsettled')
        else: b['unsettled']=values[shape]
        case['expect']['waitersComplete']=shape=='exact' and age<=30
        case['property']='no-false-doubt' if case['expect']['waitersComplete'] else 'no-false-certainty'
        yield case

    # Single benign variations are independent of one another. Exact values come
    # from these raw inputs, never from a web view function under test.
    variants=[('base',None),('more-model',None),('more-job',None),('alias',None),('idle-lane',None),('empty',None)]
    variants += [('recent-success',age) for age in (0,1,3.0001,10,1799.9999,1800)]
    variants += [('boundary',source) for source in ('snapshot','model','journal','worker','gpu','memory','peer')]
    healthy=[]
    for variant, value in variants:
        case=item('healthy-exact',dict(variation=variant,value=value)); d=case['snapshot']
        keys=['openai/gpt-oss-20b','qwen/qwen3.8-27b']+(['google/gemma-3-4b'] if variant=='more-model' else [])
        aliases={key: ('gpt-alias' if variant=='alias' and i==0 else key) for i,key in enumerate(keys)}
        api={'models':[{'key':key,'loaded_instances':[{'id':aliases[key]}]} for key in keys]}
        cli={'models':[{'identifier':aliases[key],'modelKey':key,'status':'generating' if i==0 else 'idle','queued':0} for i,key in enumerate(keys)]}
        if variant=='empty': api['models']=[]; cli['models']=[]; keys=[]
        rows={r['id']:r for r in telemetry._parse_api(api,now)}
        telemetry._merge_cli_rows(rows,telemetry._parse_lms(cli,now)); d['models']=list(rows.values())
        d['localCallers']=[] if variant=='empty' else [{'name':'Codex','connections':1}]
        d['windowsWorker']['headless']={'state':'off'}
        d['windowsWorker']['workerVersion']='1.2.3'
        d['windowsWorker']['lanes']['fast']['slotsBusy']=1
        d['windowsWorker']['gpus']=[{'index':0,'name':'NVIDIA Fixture','utilizationPercent':40,'memoryUsedMiB':1024,'memoryTotalMiB':24576,'temperatureC':55,'powerW':100}]
        d['macGpu'].update(model='Apple M5',cores=40,inUseBytes=1<<29,rendererPercent=30,tilerPercent=20,unifiedMemoryCeilingBytes=48<<30)
        d['memory'].update(swapUsedBytes=0,compressedBytes=1<<30,gpuAllocBytes=1<<30,consumers=[])
        d['peers'][0].update(platform='macos',source='agiw-peers',modelsComplete=True,health='ok',edition='mac',version='1.2.3')
        d['sources'].append({'id':'agiw-peers','state':'live','scanComplete':True,'observedAgeSeconds':0})
        d['onlineCodeMode'].update(setupState='fresh',setupAgeSeconds=1,observedAt='2027-01-15T08:00:00Z')
        b=jobs(case,results=0 if variant=='empty' else 2,opens=int(variant=='more-job'),event_age=value if variant=='recent-success' else 10)
        if variant=='boundary':
            if value=='snapshot': d['sampledAt']=d['fullSampledAt']=now-2.9999
            elif value=='model':
                for row in d['models']: row['ageSeconds']=2.9999
            elif value=='journal': next(s for s in d['sources'] if s['id']=='windows-jobs')['observedAgeSeconds']=29.9999
            elif value=='worker': d['windowsWorker']['ageSeconds']=59.9999
            elif value=='gpu': d['macGpu']['ageSeconds']=4.9999
            elif value=='memory': d['memory']['ageSeconds']=2.9999
            elif value=='peer': d['peers'][0]['ageSeconds']=29.9999
        if variant=='idle-lane':
            d['windowsWorker']['lanes']['fast']['slotsBusy']=0
            d['windowsWorker']['lanes']['deep']['slotsBusy']=0
        case['expect']['exact']={'loaded':len(keys),'active':int(bool(keys)),'queued':0,'discovered':len(keys),
                                 'callers':int(bool(keys)),'inFlight':len(b['inFlight']),'answered':len(b['recent']),
                                 'lastCurrent':bool(b['lastSuccess']),'slots':{'fast':0 if variant=='idle-lane' else 1,'deep':0}}
        healthy.append(case); yield case

    for event_age, observation_age in itertools.product((3.0001,10,1799.9999,1800),(0,2.9999,29.9999)):
        case=item('journal-observation',dict(eventAge=event_age,observationAge=observation_age))
        jobs(case,results=1,event_age=event_age,observed_age=observation_age)
        case['expect']['activityExact']={'In flight':'0','Answered':'1','Failed':'0','Routes':'0'}
        yield case

    for count, state in itertools.product((0,1,2),('unavailable','error')):
        case=item('loaded-only-catalog',dict(rows=count,apiState=state),'no-false-certainty')
        d=case['snapshot']; d['models']=d['models'][:count]
        for s in d['sources']:
            if s['id']=='lmstudio-api': s.update(state=state,route='direct')
        case['expect']['catalogUnknown']=True
        yield case

    for failure in ('missing','denied','linked','malformed','capped','empty'):
        case=item('peer-scan',dict(scan=failure),'no-false-doubt' if failure=='empty' else 'no-false-certainty')
        with tempfile.TemporaryDirectory(prefix='peer-scan-') as tmp:
            root=Path(tmp); directory=probes.peer_dir(root)
            if failure!='missing': directory.mkdir(parents=True)
            if failure=='malformed': (directory/'mac.json').write_text('{broken')
            if failure=='capped':
                for i in range(probes.PEER_SCAN_LIMIT+1): (directory/f'x{i}.txt').write_text('x')
            with mock.patch.object(probes,'host_id',return_value='fixture-pc'), mock.patch.object(probes,'is_link_or_junction',return_value=failure=='linked'):
                if failure=='denied':
                    with mock.patch.object(probes.os,'scandir',side_effect=PermissionError(errno.EACCES,'fixture denied')):
                        peers,status=probes.read_peers(root,'fixture-pc',now)
                else: peers,status=probes.read_peers(root,'fixture-pc',now)
        expected=failure=='empty'
        case['snapshot']['host']='windows'; case['snapshot']['peers']=peers
        case['snapshot']['sources']=[s for s in case['snapshot']['sources'] if s['id']!='agiw-peers']+[status]
        if status.get('scanComplete') is not expected: case['errors'].append(f'peer scanComplete={status.get("scanComplete")!r}; expected {expected}')
        if not expected and 'No AGIW peer' in status['detail']: case['errors'].append('peer source falsely claims no peers')
        case['expect']['peerEmptyKnown']=expected
        yield case

    for failure in ('invalid','incomplete','error','missing','stale','future'):
        case=item('empty-evidence-transition',dict(after=failure),'no-false-certainty')
        jobs(case,results=0)
        next(s for s in case['snapshot']['sources'] if s['id']=='windows-jobs')['ageSeconds']=0
        case['initial']=copy.deepcopy(case['snapshot'])
        d=case['snapshot']; s=next(s for s in d['sources'] if s['id']=='windows-jobs')
        if failure=='invalid': d['windowsJobs']['journal']='invalid'
        if failure=='incomplete': d['windowsJobs']['complete']=False
        if failure=='error': s['state']='error'
        if failure=='missing': d['sources'].remove(s)
        if failure in ('stale','future'): s['observedAgeSeconds']=90 if failure=='stale' else -1
        case['expect']['emptyUnknown']=True
        yield case

    for running, queued, truncated, task in itertools.product((0,1),(1,2),(False,True),('queued','running')):
        case=item('router-count-completeness',dict(running=running,queued=queued,truncated=truncated,task=task),
                  'no-false-certainty' if truncated else 'no-false-doubt')
        d=case['snapshot']; d['onlineCodeMode'].update(state='processing',active=True,routeId='route-a',
            taskState=task,runCounts={'running':running,'queued':queued,'unresolved':0},runsTruncated=truncated)
        case['expect']['routeCounts']={'running':running,'queued':queued,'truncated':truncated}
        yield case

    for original, elapsed, paused in itertools.product(healthy,(0,90),(True,False)):
        case=copy.deepcopy(original); case.update(property='frozen-capture',dimension='paused-view',
             label=f'paused-view:{original["label"]}:{elapsed}:{paused}',paused=paused,connected=paused,clockOffset=elapsed)
        case['input']={**original['input'],'elapsed':elapsed,'paused':paused}; case['expect']={'frozen':True}
        case['snapshot']['onlineCodeMode'].update(state='processing',active=True,routeId='route-a',taskState='running',
            runCounts={'running':1,'queued':0,'unresolved':0})
        yield case

    for source, epsilon, elapsed in itertools.product(('peer','worker','memory','gpu','journal','model','callers'),(-.0001,0,.0001),(0,1.9999,2.0001)):
        limit={'peer':30,'worker':60,'memory':3,'gpu':5,'journal':30,'model':3,'callers':3}[source]
        total=limit+epsilon
        case=item('transport-age-boundary',dict(source=source,totalAge=total,elapsed=elapsed),
                  'no-false-doubt' if epsilon<=0 else 'no-false-certainty')
        d=case['snapshot']; d['sampledAt']=d['fullSampledAt']=now-elapsed
        if source=='peer':
            doc={'schemaVersion':1,'kind':'agiw-peer','host':'fixture-peer','platform':'macos','observedUnix':now-total,
                 'modelsTotal':1,'models':[{'id':'model','loadedState':'loaded'}]}
            d['peers']=[probes._validate_peer(doc,now-elapsed)]
        elif source=='worker':
            cache={'checkedMonotonic':0,'probeRunning':True,'heartbeatUnix':now-total,'modelsAdvertised':['a','b'],'modelCount':2,
                   'lanes':copy.deepcopy(d['windowsWorker']['lanes'])}
            with mock.patch.object(worker_telemetry,'_WINDOWS_WORKER_CACHE',cache):
                d['windowsWorker'],status=worker_telemetry._windows_worker(now-elapsed)
        elif source=='journal':
            jobs(case,event_age=10,observed_age=(now-elapsed)-(now-total))
        elif source=='callers':
            next(s for s in d['sources'] if s['id']=='local-callers')['ageSeconds']=(now-elapsed)-(now-total)
        elif source=='model':
            for row in d['models']: row['ageSeconds']=(now-elapsed)-(now-total)
        else:
            d['memory' if source=='memory' else 'macGpu']['ageSeconds']=(now-elapsed)-(now-total)
        case['expect']['ageBoundary']={'source':source,'known':epsilon<=0}
        yield case
