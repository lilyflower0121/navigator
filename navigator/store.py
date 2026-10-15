"""One local SQLite store. Transactions fence workers and deletion together."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import os
from .contracts import APIError, LIMITS, canonical, digest

def uid():
    return uuid.uuid4().hex

class Store:
    def __init__(self,path):
        if str(path) != ':memory:':
            target=Path(path)
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            # SQLite derives WAL/SHM permissions from the main file. Set its
            # mode before connecting and creating any sidecar with source bytes.
            descriptor=os.open(target,os.O_RDWR|os.O_CREAT,0o600)
            try:
                os.fchmod(descriptor,0o600)
            finally:
                os.close(descriptor)
        self.lock=threading.RLock()
        self.request_local=threading.local()
        # Optional local project policy; the host sets these after schema init.
        # No request actor is stored on this shared Store object.
        self.resource_access=None
        self.resource_edit=None
        self.worker_access=None
        self.parse_cache={}
        self.extraction_cache={}
        self.db=sqlite3.connect(str(path),timeout=5,check_same_thread=False,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
          PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA secure_delete=ON;
          CREATE TABLE IF NOT EXISTS assets(tenant TEXT,id TEXT,deleted INTEGER DEFAULT 0,PRIMARY KEY(tenant,id));
          CREATE TABLE IF NOT EXISTS sources(tenant TEXT,asset TEXT,version INTEGER,sha TEXT,raw BLOB,meta TEXT,
            PRIMARY KEY(tenant,asset,version),FOREIGN KEY(tenant,asset) REFERENCES assets(tenant,id));
          CREATE TABLE IF NOT EXISTS jobs(tenant TEXT,id TEXT,asset TEXT,version INTEGER,actor TEXT,auth_revision INTEGER,
            state TEXT,revision INTEGER,epoch INTEGER,attempt INTEGER,lease REAL,error TEXT,result TEXT,
            PRIMARY KEY(tenant,id));
          CREATE TABLE IF NOT EXISTS segments(tenant TEXT,asset TEXT,version INTEGER,id TEXT,analysis TEXT,data TEXT,
            PRIMARY KEY(tenant,id),FOREIGN KEY(tenant,asset,version) REFERENCES sources(tenant,asset,version) ON DELETE CASCADE);
          CREATE TABLE IF NOT EXISTS records(tenant TEXT,kind TEXT,id TEXT,data TEXT,PRIMARY KEY(tenant,kind,id));
          CREATE TABLE IF NOT EXISTS dependencies(tenant TEXT,kind TEXT,id TEXT,asset TEXT,
            PRIMARY KEY(tenant,kind,id,asset));
          CREATE TABLE IF NOT EXISTS idempotency(tenant TEXT,actor TEXT,key TEXT,hash TEXT,status INTEGER,data TEXT,
            PRIMARY KEY(tenant,actor,key));
          CREATE TABLE IF NOT EXISTS idempotency_headers(tenant TEXT,actor TEXT,key TEXT,data TEXT,
            PRIMARY KEY(tenant,actor,key));
          CREATE TABLE IF NOT EXISTS sequences(tenant TEXT,process TEXT,stream TEXT,sequence INTEGER,snapshot_hash TEXT,
            PRIMARY KEY(tenant,process,stream,sequence));
          CREATE INDEX IF NOT EXISTS queued_jobs ON jobs(state,lease);
          CREATE INDEX IF NOT EXISTS segment_sources ON segments(tenant,asset,version);
          CREATE INDEX IF NOT EXISTS dependent_assets ON dependencies(tenant,asset);
        ''')
        if 'snapshot_hash' not in {r['name'] for r in self.db.execute('PRAGMA table_info(sequences)')}:
            self.db.execute('ALTER TABLE sequences ADD COLUMN snapshot_hash TEXT')
        if str(path) != ':memory:':
            for owned in (Path(path),Path(str(path)+'-wal'),Path(str(path)+'-shm')):
                if owned.exists():
                    owned.chmod(0o600)

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            self.parse_cache={}
            self.extraction_cache={}
            try:
                yield self.db
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            finally:
                self.parse_cache={}
                self.extraction_cache={}

    def close(self):
        with self.lock:
            self.db.close()

    def checkpoint(self):
        with self.lock:
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    @contextmanager
    def request_scope(self,ctx):
        previous=getattr(self.request_local,'context',None)
        self.request_local.context=ctx
        try:
            yield
        finally:
            self.request_local.context=previous

    def asset_readable(self,db,tenant,asset):
        ctx=getattr(self.request_local,'context',None)
        # Direct in-process use is a trusted local maintenance boundary. Every
        # API request installs its authenticated context, including cache reads.
        return ctx is None or (ctx['tenant']==tenant and
            (self.resource_access is None or self.resource_access(db,tenant,ctx['actor'],asset)))

    def check_asset_access(self,db,tenant,asset):
        if not self.asset_readable(db,tenant,asset):
            raise APIError(403,'permission_revoked')

    def check_asset_edit(self,db,ctx,asset,action):
        self.live(db,ctx['tenant'],asset)
        if self.resource_edit is not None and not self.resource_edit(db,ctx['tenant'],ctx['actor'],asset,action):
            raise APIError(403,'project_role_denied')

    def check_dependencies(self,db,tenant,kind,id):
        for row in db.execute('SELECT asset FROM dependencies WHERE tenant=? AND kind=? AND id=?',(tenant,kind,id)):
            self.live(db,tenant,row['asset'])

    def live(self,db,tenant,asset):
        row=db.execute('SELECT deleted FROM assets WHERE tenant=? AND id=?',(tenant,asset)).fetchone()
        if row is None:
            raise APIError(404,'not_found')
        if row['deleted']:
            raise APIError(410,'source_deleted')
        self.check_asset_access(db,tenant,asset)

    def record(self,db,tenant,kind,id):
        row=db.execute('SELECT data FROM records WHERE tenant=? AND kind=? AND id=?',(tenant,kind,id)).fetchone()
        if row is None:
            raise APIError(404,'not_found')
        import json
        value=json.loads(row['data'])
        if value.get('state')=='invalidated':
            raise APIError(410,'deleted_dependency')
        self.check_dependencies(db,tenant,kind,id)
        return value

    def save_record(self,db,tenant,kind,value,assets=()):
        maximum=LIMITS['definitions'] if kind in ('templates','processes') else LIMITS['derived_records']
        count=db.execute('SELECT count(*) FROM records WHERE tenant=? AND kind=?',(tenant,kind)).fetchone()[0]
        if count>=maximum:
            raise APIError(413,'record_capacity_limit')
        db.execute('INSERT INTO records VALUES(?,?,?,?)',(tenant,kind,value['id'],canonical(value)))
        for asset in set(assets):
            db.execute('INSERT INTO dependencies VALUES(?,?,?,?)',(tenant,kind,value['id'],asset))
        return value

    def mutate(self,ctx,key,operation,body,fn,authorize=None,response_headers=None):
        """Cache only inside the same transaction as the write; current auth first."""
        import re,json
        if not isinstance(key,str) or not re.fullmatch('[A-Za-z0-9_-]{8,80}',key):
            raise APIError(422,'idempotency_key_required')
        tenant,actor=ctx['tenant'],ctx['actor']
        fingerprint=digest(dict(operation=operation,body=body))
        with self.tx() as db:
            if authorize:
                authorize()
            old=db.execute('SELECT * FROM idempotency WHERE tenant=? AND actor=? AND key=?',(tenant,actor,key)).fetchone()
            if old:
                if old['hash']!=fingerprint:
                    raise APIError(409,'idempotency_conflict')
                if old['status']==410:
                    raise APIError(410,'deleted_dependency')
                self.check_dependencies(db,tenant,'idempotency',actor+':'+key)
                if response_headers is not None:
                    headers=db.execute('SELECT data FROM idempotency_headers WHERE tenant=? AND actor=? AND key=?',(tenant,actor,key)).fetchone()
                    if headers:
                        response_headers.update(json.loads(headers['data']))
                return old['status'],json.loads(old['data'])
            count=db.execute('SELECT count(*) FROM idempotency WHERE tenant=?',(tenant,)).fetchone()[0]
            if count>=LIMITS['idempotency_keys']:
                raise APIError(413,'idempotency_capacity_limit')
            status,value,assets=fn(db)
            db.execute('INSERT INTO idempotency VALUES(?,?,?,?,?,?)',(tenant,actor,key,fingerprint,status,canonical(value)))
            if response_headers is not None:
                db.execute('INSERT INTO idempotency_headers VALUES(?,?,?,?)',(tenant,actor,key,canonical(response_headers)))
            for asset in set(assets):
                db.execute('INSERT OR IGNORE INTO dependencies VALUES(?,?,?,?)',(tenant,'idempotency',actor+':'+key,asset))
            return status,value

    def job(self,db,tenant,id):
        import json
        row=db.execute('SELECT * FROM jobs WHERE tenant=? AND id=?',(tenant,id)).fetchone()
        if row is None:
            raise APIError(404,'not_found')
        self.live(db,tenant,row['asset'])
        meta=self.db.execute('SELECT meta FROM sources WHERE tenant=? AND asset=? AND version=?',(tenant,row['asset'],row['version'])).fetchone()
        processor='native-pdf-text-v1' if __import__('json').loads(meta['meta'])['media_type']=='application/pdf' else 'tesseract-image-v1' if __import__('json').loads(meta['meta'])['media_type'] in ('image/png','image/jpeg') else 'xlsx-source-rows-v3' if __import__('json').loads(meta['meta'])['media_type']=='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' else 'structured-text-v1'
        if __import__('json').loads(meta['meta']).get('xlsx_selection') is not None:processor='xlsx-selected-rows-v1'
        result=json.loads(row['result']) if row['result'] else None
        value=dict(id=row['id'],asset_id=row['asset'],source_version=row['version'],
                    state=row['state'],revision=row['revision'],epoch=row['epoch'],attempt=row['attempt'],
                    lease_until=row['lease'],processor_version=processor,model_used=(result or {}).get('model_used'),
                    execution_kind='real_deterministic',error=row['error'],result=result)
        if row['state']=='cancelled':
            # Epoch revokes publication immediately. It is not evidence that a
            # parser claimed earlier has physically stopped; no external action
            # or rollback is implemented by this local job processor.
            value['cancel_effect']=dict(result_publication='revoked',
                processing_stop='not_started' if row['attempt']==0 else 'not_confirmed')
        return value

    def claim(self,authorize,now=None):
        now=time.time() if now is None else now
        with self.tx() as db:
            rows=db.execute("SELECT j.* FROM jobs j JOIN assets a ON a.tenant=j.tenant AND a.id=j.asset WHERE a.deleted=0 AND (j.state='queued' OR (j.state='running' AND j.lease<?)) ORDER BY j.rowid",(now,)).fetchall()
            for row in rows:
                if not authorize(row['tenant'],row['actor'],row['auth_revision']) or (self.worker_access is not None and not self.worker_access(row['tenant'],row['actor'],row['asset'])):
                    db.execute("UPDATE jobs SET state='blocked',error='authorization_revoked',revision=revision+1,epoch=epoch+1,lease=0 WHERE tenant=? AND id=?",(row['tenant'],row['id']))
                    continue
                if row['attempt']>=3:
                    db.execute("UPDATE jobs SET state='failed',error='attempt_limit',revision=revision+1,epoch=epoch+1,lease=0 WHERE tenant=? AND id=?",(row['tenant'],row['id']))
                    continue
                db.execute("UPDATE jobs SET state='running',revision=revision+1,epoch=epoch+1,attempt=attempt+1,lease=? WHERE tenant=? AND id=?",(now+30,row['tenant'],row['id']))
                source=db.execute('SELECT raw,sha,meta FROM sources WHERE tenant=? AND asset=? AND version=?',(row['tenant'],row['asset'],row['version'])).fetchone()
                return dict(tenant=row['tenant'],id=row['id'],asset=row['asset'],version=row['version'],epoch=row['epoch']+1,
                            actor=row['actor'],auth_revision=row['auth_revision'],raw=bytes(source['raw']),sha=source['sha'],meta=__import__('json').loads(source['meta']))
        return None

    def finish(self,claim,result,authorize):
        with self.tx() as db:
            row=db.execute('SELECT state,epoch,lease FROM jobs WHERE tenant=? AND id=?',(claim['tenant'],claim['id'])).fetchone()
            if not row or row['state']!='running' or row['epoch']!=claim['epoch'] or row['lease']<time.time():
                return False
            if not authorize(claim['tenant'],claim['actor'],claim['auth_revision']) or (self.worker_access is not None and not self.worker_access(claim['tenant'],claim['actor'],claim['asset'])):
                result=dict(state='blocked',error='authorization_revoked',segments=[])
            state=result['state']
            summary={k:v for k,v in result.items() if k not in ('state','error','segments')}
            if state=='succeeded':
                for segment in result['segments']:
                    db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)',(claim['tenant'],claim['asset'],claim['version'],segment['id'],result['analysis_version'],canonical(segment)))
                summary['segments']=result['segments']
            db.execute('UPDATE jobs SET state=?,error=?,result=?,revision=revision+1,lease=0 WHERE tenant=? AND id=?',
                       (state,result.get('error'),canonical(summary),claim['tenant'],claim['id']))
            return True
