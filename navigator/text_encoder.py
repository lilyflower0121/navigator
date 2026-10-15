"""Opt-in, existing local safetensors only; CPU text encoder, no model answers.

The host supplies an administrator-selected snapshot, never an API request path.
Default API composition does not import Torch or load either model.
"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import time

from .contracts import APIError
from .retrieval import CHUNK_POLICY, FUSION, MAX_CHUNKS

PROFILES = {
    'e5': dict(model_id='intfloat/multilingual-e5-small',
               revision='614241f622f53c4eeff9890bdc4f31cfecc418b3', dimension=384,
               weight_sha256='1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477',
               query_prefix='query: ', document_prefix='passage: ', context=512, model_type='bert'),
    'gemma2': dict(model_id='google/embeddinggemma-2',
                   revision='914f7f89142e33e77833254d9c9b90c3cef7303b', dimension=768,
                   weight_sha256='197a32965d4b1105faf060417baa899e193fb73cd401f42ec9295234d5553d79',
                   query_prefix='task: search result | query: ',
                   document_prefix='title: {source_title} | text: ', context=8192,
                   model_type='embedding_gemma2'),
}
PINNED_FILES = {
    'e5': {
        'config.json':'69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959',
        'tokenizer.json':'0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39',
        'tokenizer_config.json':'a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b',
        'special_tokens_map.json':'d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7',
        'sentencepiece.bpe.model':'cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865',
    },
    'gemma2': {
        'config.json':'dc524b2cbef566d66d8533c67dc68a6a8f5d340a',
        'config_sentence_transformers.json':'ba7dd4d5481cbaf271d5e8d1ad4271c5a45d2fd7',
        'tokenizer_config.json':'a9afd12fb3ca2c418fd405320ace85ad46748d03',
        'tokenizer.json':'4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4',
        'tokenizer.model':'e594c8a90eb08d8bda498ff4747977dc827ae0c3c56b5c0d41a605a22d02ef03',
    },
}


class LocalTextEncoder:
    def __init__(self, profile, model_dir):
        if profile not in PROFILES:
            raise ValueError('unsupported_local_encoder')
        p = PROFILES[profile]
        directory = Path(model_dir).resolve(strict=True)
        weight = directory/'model.safetensors'
        # Exact previously acquired checkpoint. No pickle fallback, remote code,
        # download or caller-supplied model identity is accepted.
        with weight.open('rb') as source:
            weight_hash = hashlib.file_digest(source, 'sha256').hexdigest()
        if weight_hash != p['weight_sha256']:
            raise ValueError('local_model_weight_integrity_failed')
        file_hashes={}
        for filename, expected in PINNED_FILES[profile].items():
            data=(directory/filename).read_bytes()
            # Previously frozen official Git blob OIDs or LFS SHA256.
            actual=(hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
                    if len(expected)==40 else hashlib.sha256(data).hexdigest())
            if actual!=expected:
                raise ValueError('local_model_config_integrity_failed')
            file_hashes[filename]=hashlib.sha256(data).hexdigest()
        self.metadata = {key:p[key] for key in ('model_id','revision','dimension','query_prefix','document_prefix')}
        self.metadata.update(precision='float32', chunk_policy=CHUNK_POLICY, metric='cosine', fusion=FUSION)
        self.profile, self.context = profile, p['context']
        self.receipt = dict(weight_sha256=weight_hash, device='cpu', threads=1,
                            remote_code=False, local_files_only=True, input_truncation=False,
                            model_answers=False)
        self.receipt['configuration_sha256']=file_hashes
        os.environ['HF_HUB_OFFLINE']='1'
        os.environ['TRANSFORMERS_OFFLINE']='1'
        os.environ['TOKENIZERS_PARALLELISM']='false'
        os.environ['OMP_NUM_THREADS']='1'
        os.environ['MKL_NUM_THREADS']='1'
        t = time.perf_counter()
        import torch
        from transformers import AutoConfig, AutoModel, AutoTokenizer
        if profile=='gemma2' and importlib.metadata.version('transformers')!='5.19.0':
            raise ValueError('gemma2_requires_existing_transformers_5_19_0')
        torch.set_num_threads(1)
        torch.set_num_interop_threads(1)
        self.torch = torch
        config = AutoConfig.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
        if config.model_type != p['model_type']:
            raise ValueError('local_encoder_architecture_mismatch')
        if profile=='gemma2':
            prompts = json.loads((directory/'config_sentence_transformers.json').read_text())['prompts']
            if prompts['SearchQuery']!=p['query_prefix'] or prompts['Document']!='title: none | text: ':
                raise ValueError('local_encoder_prompt_mismatch')
            config.vision_config = None
            config.audio_config = None
        self.tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
        model, loading = AutoModel.from_pretrained(directory, config=config, local_files_only=True,
            trust_remote_code=False, use_safetensors=True, dtype=torch.float32,
            output_loading_info=True, ignore_mismatched_sizes=False)
        if loading.get('missing_keys') or loading.get('mismatched_keys') or loading.get('error_msgs'):
            raise ValueError('local_encoder_incomplete_weights')
        unexpected = loading.get('unexpected_keys', [])
        if profile=='e5' and unexpected == ['embeddings.position_ids']:
            from safetensors import safe_open
            with safe_open(weight, framework='pt') as saved:
                if not torch.equal(saved.get_tensor('embeddings.position_ids'), model.embeddings.position_ids):
                    raise ValueError('local_encoder_buffer_mismatch')
        elif profile=='gemma2':
            if any(not any(tag in key.lower() for tag in ('vision','audio','image','multimodal')) for key in unexpected):
                raise ValueError('local_encoder_unexpected_text_weights')
        elif unexpected:
            raise ValueError('local_encoder_unexpected_weights')
        self.model = model.to(device='cpu', dtype=torch.float32).eval()
        if profile=='gemma2' and any('vision' in name or 'audio' in name for name,_ in model.named_parameters()):
            raise ValueError('local_encoder_multimodal_parameters_retained')
        self.receipt.update(load_seconds=time.perf_counter()-t,
                            parameter_count=sum(param.numel() for param in model.parameters()),
                            versions={name:importlib.metadata.version(name) for name in ('torch','transformers','numpy','safetensors')})

    def _embed(self, texts):
        if not 1<=len(texts)<=MAX_CHUNKS or any(not isinstance(text,str) or len(text)>1400 for text in texts):
            raise APIError(413, 'text_encoder_input_limit')
        vectors=[]
        # One source chunk at a time bounds temporary attention memory. Prefix
        # and actual title count towards the context limit; never truncate.
        for text in texts:
            encoded=self.tokenizer([text], return_tensors='pt', padding=True, truncation=False)
            if encoded['input_ids'].shape[1]>self.context:
                raise APIError(422,'text_encoder_context_limit')
            with self.torch.inference_mode():
                hidden=self.model(**encoded).last_hidden_state
                if hidden.shape[-1]!=self.metadata['dimension']:
                    raise APIError(409,'text_encoder_dimension_mismatch')
                mask=encoded['attention_mask'].unsqueeze(-1).to(hidden.dtype)
                pooled=(hidden*mask).sum(dim=1)/mask.sum(dim=1).clamp(min=1)
                norm=pooled.norm(dim=1,keepdim=True)
                if not self.torch.isfinite(pooled).all() or (norm==0).any():
                    raise APIError(409,'text_encoder_non_finite')
                vectors.extend((pooled/norm).float().cpu().tolist())
        return vectors

    def documents(self, documents):
        if self.profile=='gemma2':
            texts=['title: '+title+' | text: '+text for title,text in documents]
        else:
            texts=['passage: '+title+'\n'+text for title,text in documents]
        return self._embed(texts)

    def query(self, text):
        return self._embed([self.metadata['query_prefix']+text])


class IsolatedTextEncoder:
    """Existing encoder in one bounded child per call; no parent model residency."""
    def __init__(self,profile,model_dir):
        if profile not in PROFILES:raise ValueError('unsupported_local_encoder')
        from .local_decision import LOCAL_MODEL_LOCK
        self.profile,self.model_dir=profile,str(Path(model_dir).resolve(strict=True))
        p=PROFILES[profile]
        self.metadata={k:p[k] for k in ('model_id','revision','dimension','query_prefix','document_prefix')}
        self.metadata.update(precision='float32',chunk_policy=CHUNK_POLICY,metric='cosine',fusion=FUSION)
        self.execution=dict(mode='isolated-cpu-v1',weight_sha256=p['weight_sha256'])
        from collections import OrderedDict
        self.lock=LOCAL_MODEL_LOCK;self.worker_receipts=[];self.query_cache=OrderedDict()
    def _call(self,kind,payload):
        import subprocess,sys
        from .contracts import canonical,digest
        from .retrieval import checked_vectors
        encoded=canonical(dict(profile=self.profile,model_dir=self.model_dir,kind=kind,payload=payload))
        if len(encoded.encode())>500000:raise APIError(413,'isolated_encoder_byte_limit')
        env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1')
        start=time.monotonic()
        with self.lock:
            cache_key=digest(payload)
            if kind=='query' and cache_key in self.query_cache:return [list(v) for v in self.query_cache[cache_key]]
            p=None
            try:
                p=subprocess.Popen([sys.executable,'-m','navigator.text_encoder','--worker'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
                stdout,stderr=p.communicate(encoded,timeout=90)
                if p.returncode:raise ValueError('worker_failed')
                result=json.loads(stdout)
                if result['metadata']!=self.metadata:raise ValueError('worker_identity')
                vectors=checked_vectors(result['vectors'],len(payload) if kind=='documents' else 1,self.metadata['dimension'])
                receipt=dict(kind=kind,input_sha256=digest(payload),input_count=len(vectors),worker_exited=True,exit_code=0,wall_seconds=time.monotonic()-start,**result['receipt'])
                self.worker_receipts.append(receipt);self.worker_receipts=self.worker_receipts[-20:]
                if kind=='query':
                    self.query_cache[cache_key]=vectors
                    while len(self.query_cache)>32:self.query_cache.popitem(last=False)
                return [list(v) for v in vectors]
            except subprocess.TimeoutExpired:
                p.kill();p.communicate();raise APIError(503,'isolated_encoder_wall_limit')
            except (OSError,ValueError,KeyError,TypeError):
                if p is not None and p.poll() is None:p.kill();p.communicate()
                raise APIError(503,'isolated_encoder_failed')
    def documents(self,documents):return self._call('documents',documents)
    def query(self,text):return self._call('query',text)


def _isolated_worker():
    import resource,sys,threading
    from .contracts import canonical
    def rss():return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
    def guard():
        while True:
            if rss()>3*1024**3:os._exit(73)
            time.sleep(.2)
    threading.Thread(target=guard,daemon=True).start()
    text=sys.stdin.read(500001)
    if len(text.encode())>500000:raise ValueError('input_limit')
    body=json.loads(text);encoder=LocalTextEncoder(body['profile'],body['model_dir'])
    if body['kind']=='documents':vectors=encoder.documents(body['payload'])
    elif body['kind']=='query':vectors=encoder.query(body['payload'])
    else:raise ValueError('operation')
    print(canonical(dict(metadata=encoder.metadata,receipt=dict(encoder.receipt,max_rss_bytes=rss(),resident_after_exit=False),vectors=vectors)))

if __name__=='__main__':
    import sys
    if sys.argv[1:]!=['--worker']:raise SystemExit('bounded encoder worker only')
    _isolated_worker()
