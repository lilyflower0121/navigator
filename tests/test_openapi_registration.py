"""Standalone contract export must resolve every schema without API import."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class OpenAPIRegistrationTests(unittest.TestCase):
    def test_standalone_export_matches_runtime_and_resolves_every_ref(self):
        source='''
from navigator.contracts import openapi,canonical
spec=openapi()
refs=[]
def walk(value):
    if isinstance(value,dict):
        if '$ref' in value: refs.append(value['$ref'])
        for child in value.values():walk(child)
    elif isinstance(value,list):
        for child in value:walk(child)
walk(spec)
assert all(ref.startswith('#/components/schemas/') and ref.rsplit('/',1)[-1] in spec['components']['schemas'] for ref in refs)
before=canonical(spec)
from navigator.api import API
assert before==canonical(openapi())
'''
        result=subprocess.run([sys.executable,'-c',source],cwd=Path(__file__).resolve().parents[1],
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':
    unittest.main()
