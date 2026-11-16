"""v2 meter-reading verification and monthly invoice ledger tests."""
import json, os, tempfile, unittest
from pathlib import Path
from tariff_rating import ledger as L
from tariff_rating.config import BatchRejected

DAY=24*3600
ANCHORS=['2025-01-01T00:00:00Z','2025-02-01T00:00:00Z',
         '2025-03-01T00:00:00Z','2025-04-01T00:00:00Z']
ANCHOR_TS=[1735689600,1738368000,1740787200,1743465600]

def ts(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def base_config(accounts=('A',), meters=(('M1','A'),), exchanges=(),
                start=ANCHORS[0], end=ANCHORS[3],
                interval=28*DAY, rate='10'):
    return {
        'currency':'TEST','fixed_charge':50,
        'tiers':[{'up_to':100,'rate':2},{'up_to':200,'rate':3},{'up_to':None,'rate':5}],
        'accounts':[{'id':a} for a in accounts],
        'meters':[{'id':m,'account':o} for m,o in meters],
        'period_start':start,'period_end':end,
        'max_interval_seconds':interval,'max_rate_kwh_per_hour':rate,
        'exchanges':[{'at':at,'out_meter':om,'in_meter':nm} for at,om,nm in exchanges],
    }

def rd(rid,meter,when,value,revision=1):
    return {'reading_id':rid,'meter':meter,'timestamp':when,'value':value,'revision':revision}

def batch(bid,seq,readings,config=None):
    return {'batch_id':bid,'seq':seq,'config':config,'readings':readings}

def dense_chain(values,meter='M1'):
    """Anchor + mid-month readings, 15-day spacing (interval limit is 28 days)."""
    out=[]
    for i,v in enumerate(values):
        out.append(rd('r%d'%i,meter,ANCHORS[i],v))
    for i in range(len(values)-1):
        out.append(rd('m%d'%i,meter,ts(ANCHOR_TS[i]+15*DAY),(values[i]+values[i+1])//2))
    return out

class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory();self.path=Path(self.td.name)/'ledger.json'
        self.state=L.new_state()
    def tearDown(self):self.td.cleanup()
    def ingest(self,data):return L.ingest(self.state,self.path,data)
    def close(self,account,pid):return L.close(self.state,self.path,account,pid)
    def status(self,account=None):return L.status(self.state,account)
    def slot(self,account='A',i=0):return self.status(account)['accounts'][account]['periods'][i]
    def reload(self):
        self.state=L.load(self.path);return self.state

class BatchIdentity(LedgerCase):
    def test_replay_returns_stored_result_and_same_generation(self):
        b=batch('b1',1,dense_chain([0,10,20,30]),base_config())
        first=self.ingest(b);second=self.ingest(b)
        self.assertFalse(first['replayed']);self.assertTrue(second['replayed'])
        self.assertEqual(second['generation'],first['generation'])
        self.assertEqual(second['sha256'],first['sha256'])
        self.reload()
        third=self.ingest(b)  # replay against a freshly loaded ledger file
        self.assertTrue(third['replayed']);self.assertEqual(third['generation'],1)
    def test_same_id_different_seq_conflicts(self):
        self.ingest(batch('b1',1,dense_chain([0,10]),base_config()))
        with self.assertRaises(BatchRejected):
            self.ingest(batch('b1',2,[rd('x','M1',ANCHORS[2],20)]))
        self.assertEqual(self.status()['generation'],1)
    def test_same_seq_different_id_conflicts(self):
        self.ingest(batch('b1',1,dense_chain([0,10]),base_config()))
        with self.assertRaises(BatchRejected):
            self.ingest(batch('other',1,dense_chain([0,10]),base_config()))
    def test_same_id_seq_different_sha_conflicts(self):
        b=batch('b1',1,dense_chain([0,10]),base_config())
        self.ingest(b)
        with self.assertRaises(BatchRejected):
            self.ingest(batch('b1',1,dense_chain([0,11]),base_config()))
    def test_missing_seq_rejected(self):
        with self.assertRaises(BatchRejected):
            self.ingest({'batch_id':'b','seq':None,'config':base_config(),'readings':[]})
    def test_seq_must_be_continuous_from_one(self):
        self.ingest(batch('b1',1,dense_chain([0,10]),base_config()))
        with self.assertRaises(BatchRejected):  # gap: seq 3 before seq 2
            self.ingest(batch('b3',3,[rd('x','M1',ANCHORS[2],20)]))
        self.assertEqual(self.status()['batch_high_water'],1)
        self.ingest(batch('b2',2,[rd('x','M1',ANCHORS[2],20)]))  # next seq accepted
        self.assertEqual(self.status()['batch_high_water'],2)
    def test_illegal_identity_rejects_whole_batch(self):
        self.ingest(batch('b1',1,dense_chain([0,10]),base_config()))
        bad=batch('b2',2,[rd('r9','UNKNOWN',ANCHORS[1],1)])
        with self.assertRaises(BatchRejected):self.ingest(bad)
        self.assertEqual(self.status()['batch_count'],1)
    def test_structural_failure_leaves_file_untouched(self):
        self.ingest(batch('b1',1,dense_chain([0,10]),base_config()))
        before=self.path.read_bytes()
        with self.assertRaises(BatchRejected):  # new reading_id must start at revision 1
            self.ingest(batch('b2',2,[rd('r9','M1',ANCHORS[1],1,revision=3)]))
        self.assertEqual(self.path.read_bytes(),before)

class Revisions(LedgerCase):
    def seed(self):
        self.ingest(batch('b1',1,dense_chain([0,100,200,300]),base_config()))
    def test_stale_revision_ignored_and_recorded(self):
        self.seed()
        self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],120,revision=2)]))
        res=self.ingest(batch('b3',3,[rd('r1','M1',ANCHORS[1],999,revision=1)]))
        self.assertEqual(res['stale_count'],1)
        self.assertEqual(res['stale'][0]['known_revision'],2)
        self.assertEqual(self.slot(i=0)['quantity'],'120')  # stale value 999 had no effect
    def test_same_revision_same_content_idempotent(self):
        self.seed()
        res=self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],100)]))
        self.assertEqual(res['accepted'],0);self.assertFalse(res['replayed'])
    def test_same_revision_different_content_rejects_batch(self):
        self.seed()
        with self.assertRaises(BatchRejected):
            self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],101)]))
    def test_higher_revision_may_change_time_and_value(self):
        self.seed()
        self.ingest(batch('b2',2,[rd('r1','M1','2025-01-20T00:00:00Z',110,revision=2)]))
        self.assertEqual(self.slot(i=0)['quantity'],'110')
    def test_revision_cannot_change_meter_identity(self):
        self.seed()
        with self.assertRaises(BatchRejected):
            self.ingest(batch('b2',2,[rd('r1','M2','2025-01-20T00:00:00Z',1,revision=2)]))

class BoundaryAndExchange(LedgerCase):
    def test_shared_boundary_not_double_counted(self):
        # (left,right] accumulation: the boundary reading closes one period only.
        self.ingest(batch('b1',1,dense_chain([0,100,250,400]),base_config()))
        q=[self.slot(i=i)['quantity'] for i in range(3)]
        self.assertEqual(q,['100','150','150'])
    def test_paired_exchange_boundary_has_no_usage(self):
        cfg=base_config(meters=(('OLD','A'),('NEW','A')),
                        exchanges=(('2025-02-01T00:00:00Z','OLD','NEW'),))
        old=[rd('o0','OLD',ANCHORS[0],0),rd('om0','OLD',ts(ANCHOR_TS[0]+15*DAY),50),
             rd('o1','OLD',ANCHORS[1],100)]
        new=[rd('n1','NEW',ANCHORS[1],100),rd('nm1','NEW',ts(ANCHOR_TS[1]+15*DAY),180),
             rd('n2','NEW',ANCHORS[2],260),rd('nm2','NEW',ts(ANCHOR_TS[2]+15*DAY),330),
             rd('n3','NEW',ANCHORS[3],400)]
        self.ingest(batch('b1',1,old+new,cfg))
        q=[self.slot(i=i)['quantity'] for i in range(3)]
        self.assertEqual(q,['100','160','140'])  # no usage produced at the exchange boundary
        self.assertTrue(all(not self.slot(i=i)['held'] for i in range(3)))
    def test_unpaired_exchange_holds_adjacent_periods(self):
        cfg=base_config(meters=(('OLD','A'),('NEW','A')),
                        exchanges=(('2025-02-01T00:00:00Z','OLD','NEW'),))
        old=[rd('o0','OLD',ANCHORS[0],0),rd('om0','OLD',ts(ANCHOR_TS[0]+15*DAY),50),
             rd('o1','OLD',ANCHORS[1],100)]
        # NEW starts only in March: the Feb-1 exchange is unpaired
        new=[rd('n2','NEW',ANCHORS[2],260),rd('nm2','NEW',ts(ANCHOR_TS[2]+15*DAY),330),
             rd('n3','NEW',ANCHORS[3],400)]
        self.ingest(batch('b1',1,old+new,cfg))
        held={self.slot(i=i)['period']:self.slot(i=i)['held'] for i in range(3)}
        self.assertEqual(held,{'2025-01':True,'2025-02':True,'2025-03':False})
        with self.assertRaises(ValueError):self.close('A','2025-02')
    def test_missing_anchor_holds_period(self):
        self.ingest(batch('b1',1,[
            rd('r0','M1',ANCHORS[0],0),rd('r2','M1',ANCHORS[2],200),
            rd('r2b','M1','2025-03-15T00:00:00Z',250),rd('r3','M1',ANCHORS[3],300)],
            base_config()))
        self.assertTrue(self.slot(i=0)['held'])   # no Feb-1 anchor: Jan & Feb affected
        self.assertTrue(self.slot(i=1)['held'])
        self.assertFalse(self.slot(i=2)['held'])  # Mar chain retains both its anchors
    def test_crossing_interval_holds_every_covered_period(self):
        self.ingest(batch('b1',1,[
            rd('r0','M1',ANCHORS[0],0),rd('rX','M1','2025-01-20T00:00:00Z',50),
            rd('r3','M1',ANCHORS[3],300)],base_config()))  # Jan20 -> Apr1 spans Feb, Mar
        held=[self.slot(i=i)['held'] for i in range(3)]
        self.assertEqual(held,[True,True,True])

class RateAndGap(LedgerCase):
    def test_overspeed_boundary_strict_inequality(self):
        # Single 28-day February window, interval 28 d: 100 kWh / 10 h against
        # max 10 kWh/h is exactly at the limit and allowed; above it is held.
        cfg=base_config(start='2025-02-01T00:00:00Z',end='2025-03-01T00:00:00Z',
                        interval=28*DAY,rate='10')
        t0='2025-02-01T00:00:00Z';t1='2025-02-01T10:00:00Z';t2='2025-03-01T00:00:00Z'
        self.ingest(batch('b1',1,[rd('r0','M1',t0,0),rd('r1','M1',t1,100),rd('r2','M1',t2,100)],cfg))
        self.assertFalse(self.slot(i=0)['held'])
        self.state=L.new_state()
        self.ingest(batch('b1',1,[rd('r0','M1',t0,0),
                                  rd('r1','M1',t1,'1000001/10000'),rd('r2','M1',t2,101)],cfg))
        self.assertTrue(self.slot(i=0)['held'])
        self.assertIn('overspeed',self.slot(i=0)['reasons'])
    def test_gap_holds_only_affected_periods(self):
        cfg=base_config(interval=20*DAY)
        self.ingest(batch('b1',1,[
            rd('r0','M1',ANCHORS[0],0),rd('r1','M1','2025-01-25T00:00:00Z',100),
            rd('r2','M1',ANCHORS[1],100),rd('r2b','M1','2025-02-15T00:00:00Z',150),
            rd('r3','M1',ANCHORS[2],200),rd('r3b','M1','2025-03-15T00:00:00Z',250),
            rd('r4','M1',ANCHORS[3],300)],cfg))
        self.assertTrue(self.slot(i=0)['held'])       # Jan1 -> Jan25 is 24 days > 20
        self.assertFalse(self.slot(i=1)['held'])
        self.assertFalse(self.slot(i=2)['held'])
    def test_non_exchange_decrease_held_but_batch_accepted(self):
        res=self.ingest(batch('b1',1,dense_chain([100,90,200,300]),base_config()))
        self.assertEqual(res['accepted'],7)  # quality anomaly, not a batch rejection
        self.assertTrue(self.slot(i=0)['held'])
        self.assertIn('decrease',self.slot(i=0)['reasons'])

class CloseAndCorrections(LedgerCase):
    def seed_clean(self, values=(0,200,400,600)):
        self.ingest(batch('b1',1,dense_chain(values),base_config()))
    def test_held_period_cannot_close(self):
        self.ingest(batch('b1',1,[
            rd('r0','M1',ANCHORS[0],0),rd('r2','M1',ANCHORS[2],200),
            rd('r3','M1',ANCHORS[3],300)],base_config()))
        with self.assertRaises(ValueError):self.close('A','2025-01')
    def test_close_freezes_invoice_and_tariff_immutably(self):
        self.seed_clean()
        inv=self.close('A','2025-01')
        self.assertEqual(inv['total'],550)  # 200: 100*2+100*3 + fixed 50
        sha=inv['invoice_sha256']
        self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],250,revision=2)]))
        again=self.slot(i=0)['frozen_invoice']
        self.assertEqual(again['sha256'],sha);self.assertEqual(again['total'],550)
        with self.assertRaises(ValueError):self.close('A','2025-01')
    def test_late_correction_net_amount_and_no_double_fixed_charge(self):
        self.seed_clean()  # Jan qty 200 -> own total 550
        self.close('A','2025-01')
        # corrected Jan qty 250 -> variable 100*2+100*3+50*5=750; fixed not re-charged
        self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],250,revision=2)]))
        feb=self.slot(i=1)
        self.assertEqual(len(feb['tentative_adjustments']),1)
        self.assertEqual(feb['tentative_adjustments'][0]['amount'],750-500)  # 250 net
        self.assertEqual(self.slot(i=0)['quantity'],'250')  # open view recomputed; invoice frozen
    def test_repeated_correction_replaces_tentative_target(self):
        self.seed_clean();self.close('A','2025-01')
        self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],250,revision=2)]))
        self.ingest(batch('b3',3,[rd('r1','M1',ANCHORS[1],210,revision=3)]))
        feb=self.slot(i=1)
        # One tentative entry only; the target is replaced, not accumulated.
        # target = corrected receivable (var+fixed) - original invoice 550 - closed net 0
        from tariff_rating.rating import rate
        var210=rate([{'id':'_','quantity':210}],base_config()['tiers'],0)['bills'][0]['variable_charge']
        self.assertEqual(len(feb['tentative_adjustments']),1)
        self.assertEqual(feb['tentative_adjustments'][0]['amount'],var210+50-550)
    def test_adjustment_permanent_after_receiver_close_then_re_correction(self):
        self.seed_clean();self.close('A','2025-01')          # own total 550
        self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],250,revision=2)]))  # +250
        # The anchor revision moves 50 kWh from Feb's quantity into Jan's, so
        # Feb now rates qty 150 (var 350); its invoice also carries the +250 adjustment.
        feb_inv=self.close('A','2025-02')
        self.assertEqual(feb_inv['quantity'],150)
        self.assertEqual(feb_inv['total'],350+50+250)
        self.assertEqual(feb_inv['received_adjustments'][0]['amount'],250)
        # After the revision Jan qty is 220 (tiered variable 600, receivable 650)
        # and Feb qty is 180 (variable 440, receivable 490).  Both closed sources
        # are recomputed on their own frozen tariffs:
        #   Jan target = 650 - own invoice 550 - closed net 250 = -150
        #   Feb target = 490 - own invoice 400 - closed net 0   =  +90
        self.ingest(batch('b3',3,[rd('r1','M1',ANCHORS[1],220,revision=3)]))
        by_source={e['source']:e['amount']
                   for e in self.slot(i=2)['tentative_adjustments']}
        self.assertEqual(by_source['A#2025-01'],-150)
        self.assertEqual(by_source['A#2025-02'],90)
        self.assertEqual(self.slot(i=1)['frozen_invoice']['total'],650)  # frozen
    def test_negative_correction_flows_through_as_negative_adjustment(self):
        self.seed_clean();self.close('A','2025-01')
        self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],150,revision=2)]))  # var 350 -> -150
        self.assertEqual(self.slot(i=1)['tentative_adjustments'][0]['amount'],-150)
    def test_no_open_receiver_keeps_unallocated_and_holds_account(self):
        cfg=base_config(start='2025-02-01T00:00:00Z',end='2025-03-01T00:00:00Z')
        t0='2025-02-01T00:00:00Z';t1='2025-03-01T00:00:00Z'
        self.ingest(batch('b1',1,[rd('r0','M1',t0,0),
                                  rd('m0','M1','2025-02-15T00:00:00Z',100),
                                  rd('r1','M1',t1,200)],cfg))
        self.close('A','2025-02')  # sole period now closed
        self.ingest(batch('b2',2,[rd('r1','M1',t1,250,revision=2),
                                  rd('m0','M1','2025-02-15T00:00:00Z',125,revision=2)]))
        st=self.status()
        self.assertEqual(st['unallocated_adjustment']['A'],250)
        self.assertTrue(st['accounts']['A']['held'])
    def test_open_period_self_correction_recomputes_directly(self):
        self.seed_clean()
        self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],250,revision=2)]))
        self.assertEqual(self.slot(i=0)['quantity'],'250')
        self.assertEqual(self.slot(i=0)['tentative_adjustments'],[])
        self.assertEqual(self.close('A','2025-01')['total'],800)  # 750 variable + 50 fixed

class PendingBlocking(LedgerCase):
    def test_held_source_blocks_receivers_and_recovery_restores(self):
        self.ingest(batch('b1',1,dense_chain([0,200,400,600]),base_config()))
        self.close('A','2025-01')  # qty 200, own 550
        # move the Feb-1 anchor away: the closed source becomes unverifiable
        self.ingest(batch('b2',2,[rd('r1','M1','2025-02-02T00:00:00Z',200,revision=2)]))
        st=self.status()
        self.assertIn('A#2025-01',st['pending_sources'])
        self.assertTrue(all(p['blocked_by'] for p in st['accounts']['A']['periods']
                            if p['state']=='open'))
        with self.assertRaises(ValueError):self.close('A','2025-02')
        # restore the anchor with value 250: target +250 recomputed and block released
        self.ingest(batch('b3',3,[rd('r1','M1',ANCHORS[1],250,revision=3),
                                  rd('m0','M1',ts(ANCHOR_TS[0]+15*DAY),125,revision=3)]))
        st=self.status()
        self.assertNotIn('A#2025-01',st['pending_sources'])
        self.assertEqual(self.slot(i=1)['tentative_adjustments'][0]['amount'],250)
        self.close('A','2025-02')

class CommitAtomicity(LedgerCase):
    def test_failed_commit_replay_shows_single_generation(self):
        self.ingest(batch('b1',1,dense_chain([0,100,200,300]),base_config()))
        gen_before=self.state['generation'];disk_before=self.path.read_bytes()
        os.environ[L.FAULT_ENV]='1'
        try:
            with self.assertRaises(OSError):
                self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],120,revision=2)]))
        finally:
            del os.environ[L.FAULT_ENV]
        self.assertEqual(self.state['generation'],gen_before)  # in-memory rolled back
        self.assertEqual(self.path.read_bytes(),disk_before)   # file untouched
        res=self.ingest(batch('b2',2,[rd('r1','M1',ANCHORS[1],120,revision=2)]))
        self.assertFalse(res['replayed']);self.assertEqual(res['generation'],gen_before+1)
        self.reload()
        self.assertEqual(self.state['generation'],gen_before+1)
    def test_persisted_file_holds_no_derived_view(self):
        self.ingest(batch('b1',1,dense_chain([0,10]),base_config()))
        raw=json.loads(self.path.read_text())
        self.assertNotIn('derived',raw)
        self.assertEqual(raw['generation'],1)

class StatusAndCaps(LedgerCase):
    def test_status_contains_required_watermarks(self):
        self.ingest(batch('b1',1,dense_chain([0,10]),base_config()))
        st=self.status()
        self.assertEqual(st['generation'],1);self.assertEqual(st['batch_high_water'],1)
        self.assertIn('account_held',st);self.assertIn('unallocated_adjustment',st)
        self.assertIn('pending_sources',st)
    def test_status_account_filter(self):
        cfg=base_config(accounts=('A','B'),meters=(('M1','A'),('M2','B')))
        self.ingest(batch('b1',1,dense_chain([0,10],'M1')+dense_chain([0,20],'M2'),cfg))
        st=L.status(self.state,'B')
        self.assertEqual(set(st['accounts']),{'B'})
    def test_interval_must_not_exceed_period_seconds(self):
        with self.assertRaises(BatchRejected):
            self.ingest(batch('b1',1,[],base_config(interval=32*DAY)))
    def test_rate_must_be_positive_rational(self):
        with self.assertRaises(BatchRejected):
            self.ingest(batch('b1',1,[],base_config(rate='0')))
    def test_meter_cap_enforced(self):
        import tariff_rating.ledger as mod
        saved=mod.MAX_READINGS_PER_METER
        mod.MAX_READINGS_PER_METER=2
        try:
            self.ingest(batch('b1',1,[rd('r0','M1',ANCHORS[0],0),rd('m0','M1',ts(ANCHOR_TS[0]+15*DAY),5)],
                              base_config()))
            with self.assertRaises(BatchRejected):
                self.ingest(batch('b2',2,[rd('r9','M1',ANCHORS[1],10)]))
        finally:
            mod.MAX_READINGS_PER_METER=saved

if __name__=='__main__':
    unittest.main()
