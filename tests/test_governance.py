"""Local registered-principal review of the actual frozen native scaffold PDF.

Candidate adapter is an explicitly synthetic SQLite fixture. Source claims,
Poppler extraction, review digests and governance transactions are real.
"""
import base64
import json
from pathlib import Path
import shutil
import unittest
from navigator.api import API, Auth
from navigator.contracts import APIError, canonical, digest, validate
from navigator.governance import Governance, GOVERNANCE_RESPONSE_SCHEMAS, GOVERNANCE_SCHEMAS, etag
from navigator.store import Store


class FixtureReleases:
    def __init__(self,store):
        self.store=store
        store.db.execute('CREATE TABLE fixture_heads(tenant TEXT,project TEXT,release_id TEXT,PRIMARY KEY(tenant,project))')
        store.db.execute('CREATE TABLE fixture_releases(tenant TEXT,project TEXT,id TEXT,revision INTEGER,digest TEXT,domains TEXT,state TEXT)')
    def create(self,tenant,project,id):
        self.store.db.execute('INSERT INTO fixture_releases VALUES(?,?,?,1,?,?,?)',(tenant,project,id,digest([]),'[]','building'))
    def candidate(self,db,ctx,project,id):
        row=db.execute('SELECT * FROM fixture_releases WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,id)).fetchone()
        if row is None:
            raise APIError(404,'not_found')
        if row['state']!='building':
            raise APIError(409,'candidate_not_writable')
        return dict(revision=row['revision'],manifest_digest=row['digest'])
    def release_for_approval(self,db,ctx,project,id):
        row=db.execute('SELECT * FROM fixture_releases WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,id)).fetchone()
        if row is None:
            raise APIError(404,'not_found')
        if row['state']!='ready':
            raise APIError(409,'release_not_ready')
        return dict(revision=row['revision'],manifest_digest=row['digest'],head_revision=0)
    def published_reference_state(self,db,ctx,project):
        head=db.execute('SELECT release_id FROM fixture_heads WHERE tenant=? AND project=?',(ctx['tenant'],project)).fetchone()
        if head is None:
            return None
        row=db.execute('SELECT * FROM fixture_releases WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,head['release_id'])).fetchone()
        if row['state']!='published':
            raise APIError(409,'invalid_published_head')
        domains=[]
        for id in json.loads(row['domains']):
            domain=json.loads(db.execute('SELECT data FROM ph4_domain_versions WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,id)).fetchone()[0])
            domains.append(dict(resource_id=id,revision=domain['revision'],content_hash=domain['content_hash'],proposal_id=domain['proposal_id'],proposal_revision=domain['proposal_revision']))
        return dict(release_id=head['release_id'],domains=domains,current_use_state='resolved')
    def adopt(self,db,ctx,project,id,domain):
        row=db.execute('SELECT domains FROM fixture_releases WHERE tenant=? AND project=? AND id=?',(ctx['tenant'],project,id)).fetchone()
        domains=json.loads(row[0])+[domain['id']]
        db.execute('UPDATE fixture_releases SET revision=revision+1,digest=?,domains=? WHERE tenant=? AND project=? AND id=?',(digest(domains),canonical(domains),ctx['tenant'],project,id))



if __name__=='__main__':
    unittest.main()
