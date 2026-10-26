"""Integrated pass dimensions: independent positive signals, UI caps and clocks."""
import itertools
from pathlib import Path

import activity
from agiw_win import collect, probes
from fix10_dimensions import scenario
from fix11_dimensions import route_projection, replace_source

NOW = 1_800_000_000.0


def dimensions(healthy):
    for gpu, notification, owners, source_age in itertools.product(('busy','clear','missing'), (3,5,None), ('live','missing','stale','partial'), (0,30.001)):
        data=healthy('windows')
        live=gpu!='missing'
        hands=probes.hands_off([dict(index=0,name='Fixture GPU',utilizationPercent=90 if gpu=='busy' else 0)],
            gpu_live=live,gpu_complete=live,lane_pids=frozenset() if owners=='live' else None,notification_state=notification)
        data['handsOff']=hands
        for source in data['sources']:
            if source['id']=='pc-gpu' and not live: source['state']='unavailable'
            if source['id']=='hands-off': source.update(ageSeconds=source_age,state='live' if hands['active'] is not None else 'unavailable')
            if source['id']=='lane-processes':
                if owners=='missing': source['state']='unavailable'
                elif owners=='stale': source['ageSeconds']=31
                elif owners=='partial': source['complete']=False
        active=None if source_age>30 else True if gpu=='busy' or notification==3 else False if live and notification==5 else None
        yield scenario(data,'hands-off-signals',dict(gpu=gpu,notification=notification,owners=owners,sourceAge=source_age),active=active,rendererNow=NOW)

    for state, event_age, observation, elapsed in itertools.product(('running','queued'), (None,60), (0,29,30,30.001,None), (0,2.5)):
        data=healthy('mac')
        pipeline, source, projected=route_projection(state,event_age)
        source['observationAgeSeconds']=observation
        data['pipeline']=pipeline; data['activity']=projected
        replace_source(data,source)
        current=observation is not None and 0<=observation and observation+elapsed<=30
        yield scenario(data,'route-inspector-clock',dict(state=state,eventAge=event_age,observation=observation,elapsed=elapsed),
            current=current,state=state,rendererNow=NOW+elapsed)

    for state, checkpoint, jobs, history in itertools.product(('running','queued'), (None,0,60), (0,2), (0,10,12,14)):
        data=healthy('mac')
        data['activity']['runs']=[dict(runId='current-route',source='router-active',activity=state,status='unresolved',host='mac',ageSeconds=checkpoint)]
        data['windowsJobs']['inFlight']=[dict(id=f'open-{i}',ageSeconds=1,timeoutSeconds=600,client='codex',lane='fast',model='model-fast') for i in range(jobs)]
        data['windowsJobs']['unsettled']=jobs
        data['windowsJobs']['recent']=[dict(id=f'done-{i}',ageSeconds=i+1,state='success',client='codex',lane='fast',model='model-fast') for i in range(history)]
        yield scenario(data,'activity-ui-cap',dict(state=state,checkpoint=checkpoint,jobs=jobs,history=history),
            state=state,truncated=1+jobs+history>12,rendererNow=NOW)

    for failed, cut, mtime, dropped in itertools.product((False,True), (False,True), (None,NOW-60), (False,True)):
        data=healthy('mac'); listing=activity._Listing([Path('hidden.json')])
        listing.read_failed=failed; listing.scan_cut=cut; listing.cut_mtime=mtime
        bound=activity._history_bound((listing,),[dict(ageSeconds=120)] if dropped else [],NOW)
        data['activity']['historyBound']=bound
        data['activity']['complete']=not bound['truncated']
        floors=([0] if failed or cut else [])+([60] if mtime is not None else [])+([120] if dropped else [])
        yield scenario(data,'history-helper-read',dict(failed=failed,cut=cut,mtime=mtime,dropped=dropped),
            truncated=bool(floors),floor=min(floors) if floors else None,rendererNow=NOW)

    for age, share_age, name in itertools.product((0,45,45.04,-5,-5.04,None), (0,2.4,30,30.04), ('mac','other')):
        data=healthy('windows')
        peer=dict(host='mac',platform='macos',fresh=share_age<=30,ageSeconds=share_age,source='agiw-peers',modelsComplete=True,
            models=[dict(id='reported-model',loadedState='loaded')])
        mac=collect.mac_from_presence([peer],'mac.local')
        mac, source=collect.merge_mac_lan_observation(mac,dict(state='unreachable',name=name,reason='refused',observedAt=None if age is None else NOW-age),NOW)
        data['peers']=[peer]; data['macPeer']=mac; data['sources'].append(source)
        current=age is not None and -5<=age<=45 and name=='mac'
        yield scenario(data,'lan-observation-clock',dict(age=age,shareAge=share_age,name=name),
            current=current,linked=share_age<=30,age=max(0,age) if current else None,rendererNow=NOW)
