"""Composition root: admin-owned runtime settings, local prototype identity/store."""
import argparse
import json
from pathlib import Path
import secrets
import signal
import threading
import os
from .api import API,Auth
from .contracts import canonical,openapi
from .http import Host
from .store import Store

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['init-local','serve','openapi'])
    parser.add_argument('--db',default=os.environ.get('NAVIGATOR_DB_PATH','.local/module.sqlite'))
    parser.add_argument('--auth',default=os.environ.get('NAVIGATOR_AUTH_PATH','.local/access.json'))
    parser.add_argument('--port',type=int)
    parser.add_argument('--bind-address')
    parser.add_argument('--allowed-host',action='append',help='Exact Host authority; repeat for each allowed host')
    parser.add_argument('--allowed-origin',action='append',help='Exact http(s) origin; repeat for each allowed origin')
    parser.add_argument('--no-worker',dest='worker',action='store_false',default=None)
    parser.add_argument('--output',default='openapi.json')
    parser.add_argument('--embedding-profile',choices=['e5','gemma2'])
    parser.add_argument('--model-dir')
    parser.add_argument('--isolated-embedding',action='store_true',help='Short-lived CPU encoder workers, serialized with decoder workers')
    parser.add_argument('--decision-profile',choices=['local-qwen3-06b'],help='Opt-in pinned existing CPU model; diagnostic only; quality gate failed')
    parser.add_argument('--short-answer-profile',choices=['local-qwen3-06b'],help='Experimental offline answer candidates; no automatic semantic acceptance')
    args=parser.parse_args()
    if args.command=='openapi':
        Path(args.output).write_text(json.dumps(openapi(),ensure_ascii=False,indent=2)+'\n')
        return
    if args.command=='init-local':
        path=Path(args.auth);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        context=dict(tenant='synthetic-local',actor='local-owner',actor_type='human',revision=1,scopes=['read','write','delete','diagnose','export'],active=True)
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w') as target:
            target.write(canonical({secrets.token_urlsafe(32):context})+'\n')
        print('Local test principal saved in '+str(path)+'. Existing files were not replaced.')
        return
    principals=json.loads(Path(args.auth).read_text())
    if not principals or any(not isinstance(token,str) or len(token)<32 or not isinstance(ctx,dict)
                             or not {'tenant','actor','revision','scopes'}<=ctx.keys()
                             or not isinstance(ctx['tenant'],str) or not isinstance(ctx['actor'],str)
                             or type(ctx['revision']) is not int or ctx['revision']<1
                             or not isinstance(ctx['scopes'],list) or not set(ctx['scopes'])<=set(['read','write','delete','diagnose','export'])
                             or ctx.get('actor_type','machine') not in ('human','agent','service','machine')
                             for token,ctx in principals.items()):
        raise ValueError('invalid_local_principals')
    port=args.port if args.port is not None else int(os.environ.get('PORT','4340'))
    bind_address=args.bind_address or os.environ.get('NAVIGATOR_BIND_ADDRESS','127.0.0.1')
    def entries(name,explicit):
        if explicit is not None:
            return explicit
        value=os.environ.get(name)
        return value.split(',') if value is not None else None
    worker_setting=os.environ.get('NAVIGATOR_WORKER','1')
    if args.worker is None and worker_setting not in ('0','1'):
        raise ValueError('invalid_worker_setting')
    worker=args.worker if args.worker is not None else worker_setting=='1'
    if bool(args.embedding_profile)!=bool(args.model_dir):
        parser.error('--embedding-profile and --model-dir are required together')
    encoder=None
    if args.isolated_embedding and not args.embedding_profile:parser.error('--isolated-embedding requires an embedding profile')
    if args.embedding_profile:
        from .text_encoder import LocalTextEncoder,IsolatedTextEncoder
        encoder=(IsolatedTextEncoder if args.isolated_embedding else LocalTextEncoder)(args.embedding_profile,args.model_dir)
    decision_model=None
    if args.decision_profile:
        from .local_decision import IsolatedLocalDecision
        decision_model=IsolatedLocalDecision()
    short_answer_model=None
    if args.short_answer_profile:
        from .short_answer import IsolatedShortAnswer
        short_answer_model=IsolatedShortAnswer()
    store=Store(args.db)
    try:
        host=Host(API(store,Auth(principals),encoder,decision_model,short_answer_model),port,worker,bind_address=bind_address,
                  allowed_hosts=entries('NAVIGATOR_ALLOWED_HOSTS',args.allowed_host),
                  allowed_origins=entries('NAVIGATOR_ALLOWED_ORIGINS',args.allowed_origin))
    except Exception:
        store.close()
        raise
    done=threading.Event()
    signal.signal(signal.SIGINT,lambda *_:done.set())
    signal.signal(signal.SIGTERM,lambda *_:done.set())
    try:
        label='NAVIGATOR local modular API: ' if bind_address=='127.0.0.1' else 'NAVIGATOR container prototype API: '
        print(label+host.start(),flush=True)
        done.wait()
    finally:
        host.close();store.close()

if __name__=='__main__':
    main()
