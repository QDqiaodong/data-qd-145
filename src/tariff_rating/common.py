"""Strict local JSON contract and atomic report publication."""
from pathlib import Path
from datetime import datetime
from fractions import Fraction
import hashlib, json, os, tempfile

def require(obj,keys):
    if not isinstance(obj,dict) or set(obj)!=set(keys.split()):raise ValueError('expected object fields: '+keys)

def ident(v):
    if not isinstance(v,str) or not v or v!=v.strip() or len(v)>128 or any(ord(c)<32 for c in v):
        raise ValueError('expected nonblank identifier, <=128 characters')
    return v

def number(v,minimum=0,maximum=10**9):
    if type(v) is not int or not minimum<=v<=maximum:raise ValueError('integer outside allowed bounds')
    return v

def rows(value,limit=20000):
    if not isinstance(value,list) or len(value)>limit:raise ValueError('expected bounded array')
    return value

def read(path,max_bytes=8_000_000):
    with Path(path).open('rb') as stream:raw=stream.read(max_bytes+1)
    if len(raw)>max_bytes:raise ValueError('JSON exceeds %d bytes'%max_bytes)
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError('duplicate JSON key: '+k)
            result[k]=v
        return result
    def invalid(v):raise ValueError('nonfinite JSON number: '+v)
    data=json.loads(raw,object_pairs_hook=unique,parse_constant=invalid)
    return data,hashlib.sha256(raw).hexdigest()

def save(path,report):
    dest=Path(path)
    if dest.is_symlink():raise ValueError('output cannot be a symlink')
    dest.parent.mkdir(parents=True,exist_ok=True);temp=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=dest.parent,delete=False) as stream:
            temp=stream.name;json.dump(report,stream,sort_keys=True,ensure_ascii=False,allow_nan=False,indent=2);stream.write('\n')
        os.replace(temp,dest)
    finally:
        if temp and os.path.exists(temp):os.unlink(temp)

def canonical(obj):
    """Canonical JSON text: sorted keys, compact separators, UTF-8."""
    return json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)

def sha256_hex(obj):
    return hashlib.sha256(canonical(obj).encode('utf-8')).hexdigest()

def parse_ts(v):
    """UTC ISO-8601 second-precision timestamp -> integer epoch seconds."""
    if not isinstance(v,str) or not v:raise ValueError('expected ISO-8601 UTC timestamp string')
    text=v[:-1]+'+00:00' if v.endswith('Z') else v
    try:dt=datetime.fromisoformat(text)
    except ValueError as error:raise ValueError('bad timestamp: '+v) from error
    if dt.tzinfo is None or dt.utcoffset().total_seconds()!=0:raise ValueError('timestamp must be UTC: '+v)
    if dt.microsecond:raise ValueError('sub-second timestamps are not allowed: '+v)
    return int(dt.timestamp())

def rational(v):
    """Non-negative rational as int or decimal/fraction string -> canonical Fraction string."""
    if type(v) is int:
        if v<0:raise ValueError('rational must be non-negative')
        return str(Fraction(v))
    if not isinstance(v,str) or not v:raise ValueError('expected rational number string')
    s=v.strip()
    try:
        if '/' in s:
            a,b=s.split('/');f=Fraction(int(a),int(b))
        else:
            f=Fraction(s)
    except (ValueError,ZeroDivisionError) as error:raise ValueError('bad rational number: '+v) from error
    if f<0:raise ValueError('rational must be non-negative: '+v)
    return str(f)

def positive_rational(v):
    s=rational(v)
    if Fraction(s)==0:raise ValueError('expected positive rational number')
    return s
