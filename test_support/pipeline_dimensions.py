"""Pass 9 dimensions: real bounded journal I/O, clocks, caps, identities and UI evidence."""
import copy
import itertools
import errno
import json
from pathlib import Path
import tempfile
from unittest import mock

import online_code_repair
import telemetry


def record(now, index, event='result', offset=-1, **fields):
    return {'v': 1, 'id': f'mac-20260926-181500-{index:012x}', 'event': event,
            'unix': now + offset, 'model': 'provider/a', 'status': 'success',
            'elapsedSeconds': 1, 'lane': 'fast', 'timeout': 600, **fields}


def dimensions(base, now):
    def item(dimension, parameters):
        return {'label': dimension + ':' + json.dumps(parameters, sort_keys=True),
                'dimension': dimension, 'input': parameters, 'changes': {},
                'snapshot': copy.deepcopy(base), 'errors': [], 'expect': {}}

    def journal(case, records, *, padding=0, expected_complete=True, expected_open=0, alive=True):
        with tempfile.TemporaryDirectory(prefix='journal-dimension-') as directory:
            path = Path(directory) / 'jobs.jsonl'
            raw = b''.join(json.dumps(row).encode() + b'\n' for row in records)
            if padding:
                # Put filler after the enqueue; it is valid old settled history.
                filler = record(now, 9999, offset=-3600, padding='x' * padding)
                raw = json.dumps(records[0]).encode() + b'\n' + (json.dumps(filler).encode()+b'\n') * 100 + b''.join(json.dumps(row).encode()+b'\n' for row in records[1:])
            path.write_bytes(raw)
            path.chmod(0o600)
            case['input']['bytes'] = len(raw)
            try:
                with mock.patch.object(telemetry, '_WINDOWS_JOBS_PATH', path), mock.patch.object(telemetry, '_pid_alive', return_value=alive), mock.patch.object(online_code_repair.time, 'time', return_value=now):
                    jobs, status = telemetry._windows_jobs(now)
                    open_jobs = online_code_repair.ShareControl.__new__(online_code_repair.ShareControl).open_jobs()
                case['snapshot']['windowsJobs'] = jobs
                case['snapshot']['sources'] = [s for s in base['sources'] if s['id'] != 'windows-jobs'] + [status]
                want_open = expected_open if expected_complete else None
                for field, actual, want in [('complete', jobs['complete'], expected_complete),
                                             ('unsettled', jobs['unsettled'], want_open),
                                             ('repair.open_jobs', open_jobs, want_open)]:
                    if actual != want: case['errors'].append(f'{field}={actual!r}; expected {want!r}')
                case['expect']['jobKnown'] = expected_complete
                if not expected_complete: case['expect']['noAbsence'] = True
            except Exception as error:
                case['errors'].append(f'journal raised {type(error).__name__}: {error}')
        return case

    for padding, opens, results in itertools.product((2200, 2700, 3000), (0, 1), (1, 11)):
        rows = ([record(now, 1, 'enqueued')] if opens else [record(now, 1, offset=-3600)]) + [record(now, i+10) for i in range(results)]
        case = item('journal-tail', dict(padding=padding, opens=opens, results=results))
        yield journal(case, rows, padding=padding, expected_complete=padding==2200, expected_open=opens)
    for lines in (1999, 2000, 2001):
        case = item('journal-tail', dict(lines=lines))
        # Small valid records isolate the line limit from the byte limit.
        rows = [{'v':1,'id':f'mac-20260926-181500-{i:012x}','event':'result','unix':now-3600,'status':'success'} for i in range(lines)]
        yield journal(case, rows, expected_complete=lines<=2000)

    for offset, event, reverse in itertools.product((-1, 0, .1, 1, 5, 5.01), ('result', 'enqueued'), (False, True)):
        case = item('journal-clock', dict(offset=offset,event=event,reverse=reverse))
        rows = [record(now,1),record(now,2,event,offset)]
        yield journal(case, rows[::-1] if reverse else rows, expected_complete=offset<=0, expected_open=int(event=='enqueued' and offset<=0))

    for results, opens, age in itertools.product((0,1,9,10,11,24), (0,1,8,9,12), (1799.9999,1800,1800.0001)):
        case = item('journal-display', dict(results=results,opens=opens,age=age))
        rows = [record(now,i+1,offset=-age) for i in range(results)] + [record(now,i+100,'enqueued') for i in range(opens)]
        case['expect']['lastCurrent'] = results > 0 and age <= 1800
        yield journal(case, rows, expected_open=opens)

    for alive, opens, results in itertools.product((True,False), (1,2), (0,1)):
        case=item('waiting-client-evidence',dict(alive=alive,opens=opens,results=results))
        rows=[record(now,i+1,'enqueued',pid=12345) for i in range(opens)] + [record(now,100) for _ in range(results)]
        case=journal(case,rows,expected_open=opens,alive=alive)
        if not alive:case['expect'].update(jobKnown=False,currentUnknown=True)
        yield case

    for state, complete, freshness, rows in itertools.product(('recorded','invalid','absent'), (True,False), ('current','boundary','expired','stale','future','unknown-age','error','missing'), (0,1,2)):
        case = item('job-evidence',dict(journal=state,complete=complete,freshness=freshness,rows=rows))
        jobs={'schemaVersion':1,'journal':state,'complete':complete,'ageSeconds':0,'inFlight':[], 'recent':[], 'lastSuccess':None}
        jobs['inFlight']=[{'id':f'job-{i}','model':'provider/a','ageSeconds':1,'timeoutSeconds':600,'lane':'fast','client':'codex'} for i in range(rows)]
        jobs['unsettled']=rows
        status={'id':'windows-jobs','state':'recorded','ageSeconds':0,'observedAgeSeconds':0}
        if freshness in ('boundary','expired'): status['observedAgeSeconds']=30 if freshness=='boundary' else 30.0001
        if freshness in ('stale','future'): status['observedAgeSeconds']=90 if freshness=='stale' else -1
        if freshness=='unknown-age': status['observedAgeSeconds']=None
        if freshness=='error': status['state']='error'
        case['snapshot']['windowsJobs']=jobs
        case['snapshot']['sources']=[s for s in base['sources'] if s['id']!='windows-jobs']+([] if freshness=='missing' else [status])
        case['expect'].update(currentJobs=state=='recorded' and freshness in ('current','boundary'), jobKnown=state=='recorded' and complete and freshness in ('current','boundary'), noAbsence=not(state=='recorded' and complete and freshness in ('current','boundary')))
        yield case

    for failure, rows in itertools.product(('absent','unreadable','invalid-next-to-valid'), (0,1)):
        case=item('journal-integrity',dict(failure=failure,rows=rows))
        raw = b''.join(json.dumps(record(now,i+1,'enqueued')).encode()+b'\n' for i in range(rows))
        if failure == 'invalid-next-to-valid': raw += b'{broken}\n'
        with tempfile.TemporaryDirectory(prefix='journal-integrity-') as directory:
            path=Path(directory)/'jobs.jsonl'
            if failure!='absent': path.write_bytes(raw); path.chmod(0o600)
            with mock.patch.object(telemetry,'_WINDOWS_JOBS_PATH',path), mock.patch.object(online_code_repair.time,'time',return_value=now):
                if failure=='unreadable':
                    with mock.patch.object(telemetry.os,'open',side_effect=PermissionError(errno.EACCES,'fixture denied')):
                        jobs,status=telemetry._windows_jobs(now)
                        count=online_code_repair.ShareControl.__new__(online_code_repair.ShareControl).open_jobs()
                else:
                    jobs,status=telemetry._windows_jobs(now)
                    count=online_code_repair.ShareControl.__new__(online_code_repair.ShareControl).open_jobs()
            for field,value in [('unsettled',jobs.get('unsettled')),('repair.open_jobs',count)]:
                if value is not None:case['errors'].append(f'{field}={value!r}; expected None')
            case['snapshot']['windowsJobs']=jobs
            case['snapshot']['sources']=[s for s in base['sources'] if s['id']!='windows-jobs']+[status]
            case['expect'].update(jobKnown=False,noAbsence=True,currentJobs=False)
        yield case

    core=('openai/gpt-oss-20b','qwen/qwen3.8-27b','google/gemma-3-4b')
    for keys, aliases, duplicate in itertools.product((1,2,3), (1,2,4), (False,True)):
        case=item('canonical-aliases',dict(keys=keys,aliases=aliases,duplicate=duplicate))
        api={'models':[{'key':key,'loaded_instances':[{'id':f'{key.split("/")[-1]}:{i+1}'} for i in range(aliases)]} for key in core[:keys]]}
        cli={'models':[{'identifier':instance['id'],'modelKey':model['key'],'status':'idle','queued':0} for model in api['models'] for instance in model['loaded_instances']]}
        rows={row['id']:row for row in telemetry._parse_api(api,now)}
        telemetry._merge_cli_rows(rows,telemetry._parse_lms(cli,now))
        case['snapshot']['models']=list(rows.values()) * (2 if duplicate else 1)
        case['expect'].update(coreDiscovered=keys, runtimeStars=keys*aliases)
        yield case

    for count, limit, state in itertools.product((1,2,13), (1,2,12), ('running','queued')):
        case=item('stale-activity-bounds',dict(count=count,limit=limit,state=state))
        case['expect']['activity']={'count':count,'limit':limit,'state':state}
        yield case

    for lane, missing, jobs in itertools.product(('fast','deep'), ('slotsBusy','slotsTotal','up','model'), (0,1)):
        case=item('partial-lane-evidence',dict(lane=lane,missing=missing,jobs=jobs))
        case['snapshot']['windowsWorker']['lanes'][lane].pop(missing)
        case['expect']['partialLane']=lane
        case['snapshot']['windowsJobs']['inFlight']=([{'id':'open','model':'provider/a','ageSeconds':1,'timeoutSeconds':600,'lane':lane}] if jobs else [])
        yield case
