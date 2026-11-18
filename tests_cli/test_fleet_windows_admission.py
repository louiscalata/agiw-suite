import copy
from dataclasses import replace
import unittest
from agiw.fleet_windows_admission import BoundEvidence, assess
from agiw.fleet_routing_plan import plan, HostedEvidence


class WindowsAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.status = {'schemaVersion': 1, 'switch': {'state': 'on'}, 'usable': True,
                       'worker': {'ok': True, 'heartbeatAgeSeconds': 2},
                       'lanes': {'fixture': {'model': 'fixture-model', 'up': True,
                           'macSlotsInUse': 0, 'slotsPerLane': 1, 'macSlotsUnsafe': 0,
                           'outstandingJobs': 0, 'pcSlots': {'busy': 0, 'total': 1}}}}
        self.e = BoundEvidence(True, True, 99, 'fixture-model', True, True,
                               'fixture-job', 'fixture-model', 110)
    def run_assess(self, status=None, **changes):
        return assess(self.status if status is None else status, replace(self.e, **changes),
                      requested_model='fixture-model', job_id='fixture-job', lane='fixture', now=100)
    def denied(self, **changes):
        self.assertIsNone(self.run_assess(**changes).candidate)
    def test_candidate_integrates_with_existing_planner(self):
        result = self.run_assess()
        self.assertEqual(result.reason, 'offline-candidate-only')
        self.assertEqual(plan('local', 'gpu-heavy', {'dual-gpu-pc': result.candidate},
                              HostedEvidence(), now=100).worker, 'dual-gpu-pc')
    def test_missing_share(self): self.denied(share_present=False)
    def test_unbound_transport(self): self.denied(transport_bound=False)
    def test_stale_future_nonfinite_observation(self):
        for value in (0, 101, float('nan'), float('inf'), True, None):
            with self.subTest(value=value): self.denied(observed_at=value)
    def test_model_inventory_is_not_served_proof(self): self.denied(model_verified=False)
    def test_served_model_mismatch(self): self.denied(served_model='another-model')
    def test_reservation_required(self): self.denied(reservation_valid=False)
    def test_reservation_bound_to_job_and_model(self):
        self.denied(reservation_job='other-job'); self.denied(reservation_model='other-model')
    def test_expired_or_uncertain_lease(self):
        for value in (100, 99, float('nan'), None, True): self.denied(reservation_expires_at=value)
    def test_malformed_status(self):
        for value in ([], {}, {'schemaVersion': True}, {'schemaVersion': 1}):
            self.assertIsNone(self.run_assess(value).candidate)
    def test_owner_off_or_unknown(self):
        for group, key, value in [('switch','state','off'),('worker','ok',False),('worker','ok',1)]:
            s=copy.deepcopy(self.status); s[group][key]=value
            self.assertIsNone(self.run_assess(s).candidate)
    def test_effective_heartbeat_age(self):
        for age in (30, -1, None, float('nan'), True):
            s=copy.deepcopy(self.status); s['worker']['heartbeatAgeSeconds']=age
            self.assertIsNone(self.run_assess(s).candidate)
    def test_lane_failure_matrix(self):
        for key, value in [('model','other'),('up',False),('up',1),('outstandingJobs',1),
                           ('outstandingJobs',None),('outstandingJobs',False),('macSlotsInUse',1),
                           ('macSlotsInUse',None),('macSlotsUnsafe',1),('macSlotsUnsafe',False),('macSlotsUnsafe',None),
                           ('slotsPerLane',0),('pcSlots',None),('pcSlots',{'busy':1,'total':2}),
                           ('pcSlots',{'busy':False,'total':1}),('pcSlots',{'busy':0,'total':0})]:
            with self.subTest(key=key,value=value):
                s=copy.deepcopy(self.status); s['lanes']['fixture'][key]=value
                self.assertIsNone(self.run_assess(s).candidate)
    def test_does_not_mutate_owner_data(self):
        before=copy.deepcopy(self.status); self.run_assess(); self.assertEqual(before,self.status)
    def test_rejected_status_has_no_fallback(self):
        result=self.run_assess(transport_bound=False)
        workers={} if result.candidate is None else {'dual-gpu-pc':result.candidate}
        self.assertIsNone(plan('online','gpu-heavy',workers,HostedEvidence(True,True,True,True,True),now=100).worker)

if __name__ == '__main__': unittest.main()
