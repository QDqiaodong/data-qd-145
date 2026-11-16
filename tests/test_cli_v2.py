"""End-to-end CLI tests for ingest / status / close and legacy v1 mode."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'src'
ENV={**os.environ,'PYTHONPATH':str(SRC)}

DAY=24*3600
ANCHORS=['2025-01-01T00:00:00Z','2025-02-01T00:00:00Z',
         '2025-03-01T00:00:00Z','2025-04-01T00:00:00Z']
ANCHOR_TS=[1735689600,1738368000,1740787200,1743465600]

def ts(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def config():
    return {
        'currency':'TEST','fixed_charge':50,
        'tiers':[{'up_to':100,'rate':2},{'up_to':200,'rate':3},{'up_to':None,'rate':5}],
        'accounts':[{'id':'A'}],'meters':[{'id':'M1','account':'A'}],
        'period_start':ANCHORS[0],'period_end':ANCHORS[3],
        'max_interval_seconds':28*DAY,'max_rate_kwh_per_hour':'10','exchanges':[],
    }

def chain(values):
    out=[{'reading_id':'r%d'%i,'meter':'M1','timestamp':ANCHORS[i],
          'value':values[i],'revision':1} for i in range(len(values))]
    out += [{'reading_id':'m%d'%i,'meter':'M1','timestamp':ts(ANCHOR_TS[i]+15*DAY),
             'value':(values[i]+values[i+1])//2,'revision':1} for i in range(len(values)-1)]
    return out

class CliV2(unittest.TestCase):
    def call(self,*args):
        return subprocess.run([sys.executable,'-m','tariff_rating.cli',*map(str,args)],
                              capture_output=True,text=True,env=ENV,cwd=ROOT)
    def test_full_lifecycle_and_replay_same_ledger_path(self):
        with tempfile.TemporaryDirectory() as td:
            td=Path(td);ledger=td/'L.json';batch=td/'b1.json'
            batch.write_text(json.dumps({'batch_id':'b1','seq':1,'config':config(),
                                         'readings':chain([0,200,400,600])}))
            r=self.call('ingest','--batch',batch,'--ledger',ledger)
            self.assertEqual(r.returncode,0,r.stderr);first=json.loads(r.stdout)
            self.assertEqual(first['generation'],1);self.assertFalse(first['replayed'])
            # same path, same batch -> same committed ledger, replay result
            r=self.call('ingest','--batch',batch,'--ledger',ledger)
            self.assertEqual(r.returncode,0,r.stderr)
            replay=json.loads(r.stdout);self.assertTrue(replay['replayed'])
            self.assertEqual(replay['generation'],1)
            r=self.call('status','--ledger',ledger,'--account','A')
            self.assertEqual(r.returncode,0,r.stderr)
            st=json.loads(r.stdout)
            self.assertEqual([p['quantity'] for p in st['accounts']['A']['periods']],
                             ['200','200','200'])
            r=self.call('close','--ledger',ledger,'--account','A','--period','2025-01')
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(json.loads(r.stdout)['total'],550)
            # held period cannot be closed: drop an anchor via a new batch
            b2=td/'b2.json';b2.write_text(json.dumps({'batch_id':'b2','seq':2,'config':None,
                'readings':[{'reading_id':'r1','meter':'M1',
                             'timestamp':'2025-02-02T00:00:00Z','value':200,'revision':2}]}))
            self.assertEqual(self.call('ingest','--batch',b2,'--ledger',ledger).returncode,0)
            r=self.call('close','--ledger',ledger,'--account','A','--period','2025-02')
            self.assertEqual(r.returncode,2);self.assertIn('held',r.stderr+r.stdout)
    def test_conflict_batch_refused_with_exit_2(self):
        with tempfile.TemporaryDirectory() as td:
            td=Path(td);ledger=td/'L.json';b=td/'b.json'
            b.write_text(json.dumps({'batch_id':'b','seq':1,'config':config(),
                                     'readings':chain([0,10])}))
            self.assertEqual(self.call('ingest','--batch',b,'--ledger',ledger).returncode,0)
            b2=td/'b2.json';b2.write_text(json.dumps({'batch_id':'b','seq':2,'config':None,
                                                      'readings':[]}))
            r=self.call('ingest','--batch',b2,'--ledger',ledger)
            self.assertEqual(r.returncode,2)
    def test_status_before_ingest_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            r=self.call('status','--ledger',Path(td)/'missing.json')
            self.assertEqual(r.returncode,2)
    def test_legacy_v1_invocation_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json'
            r=self.call('--source',src,'--output',out)
            self.assertEqual(r.returncode,0,r.stderr)
            report=json.loads(out.read_text())
            self.assertEqual(report['schema_version'],1)
            self.assertEqual(report['result']['grand_total'],1100)

if __name__=='__main__':
    unittest.main()
