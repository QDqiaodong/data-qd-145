
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,itertools
from pathlib import Path
from fractions import Fraction
from tariff_rating.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['unknown']=1;self.bad(d)

    def test_bills(self):
        r=analyze(sample());self.assertEqual([b['total'] for b in r['bills']],[800,50,250]);self.assertEqual(r['grand_total'],1100);self.assertEqual([l['quantity'] for l in r['bills'][0]['lines']],[100,100,50])
    def test_boundary(self):
        d=sample();d['accounts']=[{'id':'x','quantity':200}];r=analyze(d)['bills'][0];self.assertEqual(r['variable_charge'],500);self.assertEqual(len(r['lines']),2)
    def test_zero(self):
        d=sample();d['accounts']=[{'id':'x','quantity':0}];r=analyze(d)['bills'][0];self.assertEqual(r['total'],50);self.assertEqual(r['lines'],[])
    def test_missing_unbounded(self):
        d=sample();d['tiers'].pop();self.bad(d)
    def test_unbounded_not_last(self):
        d=sample();d['tiers'][0]['up_to']=None;self.bad(d)
    def test_nonincreasing(self):
        d=sample();d['tiers'][1]['up_to']=100;self.bad(d)
    def test_duplicate_account(self):
        d=sample();d['accounts'].append(d['accounts'][0]);self.bad(d)
    def test_independent_unit_oracle(self):
        rng=random.Random(3)
        for _ in range(20):
            q=rng.randrange(301);d=sample();d['accounts']=[{'id':'x','quantity':q}];expected=50+sum(2 if i<=100 else 3 if i<=200 else 5 for i in range(1,q+1));self.assertEqual(analyze(d)['grand_total'],expected)

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','tariff_rating.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_same_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
