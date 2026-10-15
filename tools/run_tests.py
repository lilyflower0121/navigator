"""Run portable synthetic tests and save counts without publishing raw logs."""
import argparse
import io
import json
from pathlib import Path
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--summary',required=True);a.add_argument('--pattern',default='test*.py');args=a.parse_args()
    out=Path(args.summary)
    if out.exists():raise FileExistsError('Use a new summary path; prior results are preserved.')
    log=io.StringIO();start=time.monotonic()
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=args.pattern))
    report=dict(schema_version='navigator-portable-tests-v1',tests_run=result.testsRun,failures=len(result.failures),errors=len(result.errors),
                skipped=len(result.skipped),skip_reasons=[reason for _,reason in result.skipped],passed=result.wasSuccessful(),
                seconds=round(time.monotonic()-start,4),scope='synthetic software contracts; optional runtime skips explicit; no model quality measurement')
    out.parent.mkdir(parents=True,exist_ok=True)
    with out.open('x') as f:f.write(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
    if not result.wasSuccessful():print(log.getvalue(),file=sys.stderr)
    raise SystemExit(0 if result.wasSuccessful() else 1)

if __name__=='__main__':main()
