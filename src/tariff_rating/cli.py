DESCRIPTION='Exact progressive tariff rating with itemized tier charges'
import argparse, json, sys
from pathlib import Path
from .common import read, save
from .model import analyze
from . import ledger as ledger_mod

def run(source):
    data,digest=read(source)
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}

def _print(obj):
    print(json.dumps(obj,sort_keys=True,ensure_ascii=False,allow_nan=False,indent=2))

def main():
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    sub=parser.add_subparsers(dest='command')

    p_ingest=sub.add_parser('ingest',help='commit a reading batch to a ledger')
    p_ingest.add_argument('--batch',type=Path,required=True,help='batch JSON file')
    p_ingest.add_argument('--ledger',type=Path,required=True,help='ledger file path')

    p_status=sub.add_parser('status',help='read ledger status')
    p_status.add_argument('--ledger',type=Path,required=True)
    p_status.add_argument('--account',default=None)

    p_close=sub.add_parser('close',help='close a released period and freeze its invoice')
    p_close.add_argument('--ledger',type=Path,required=True)
    p_close.add_argument('--period',required=True,help='period id YYYY-MM or index')
    p_close.add_argument('--account',default=None)

    # Legacy v1 single-period invocation (no subcommand): --source/--output.
    parser.add_argument('--source',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        if args.command is None:
            if args.source is None or args.output is None:
                parser.error('legacy mode requires --source and --output')
            if args.source.resolve()==args.output.resolve():raise ValueError('input and output must differ')
            report=run(args.source);save(args.output,report)
            print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

        if args.command=='ingest':
            data,_=read(args.batch,max_bytes=64_000_000)
            state=ledger_mod.load(args.ledger) if args.ledger.exists() else ledger_mod.new_state()
            result=ledger_mod.ingest(state,args.ledger,data);_print(result);return 0

        state=ledger_mod.load(args.ledger)
        if args.command=='status':
            _print(ledger_mod.status(state,args.account));return 0
        if args.command=='close':
            if not args.account:parser.error('close requires --account')
            _print(ledger_mod.close(state,args.ledger,args.account,args.period));return 0
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    return 2

if __name__=='__main__':raise SystemExit(main())
