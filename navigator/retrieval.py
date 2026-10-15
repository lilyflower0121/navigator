"""Bounded BM25 and exact float retrieval over already authorized source text.

No vector database, persisted index, captions, model downloads or model answers.
The caller resolves every eligible segment against its original before encoding.
"""
from array import array
from collections import Counter, OrderedDict
import math
import re
import time

from .contracts import APIError, canonical, digest

CHUNK_POLICY = 'unicode-400-overlap80-v1'
FUSION = 'rrf-k60-bm25-dense-top20-v1'
MAX_CHUNKS = 2048
SCORE_MEANINGS = {
    'lexical': 'lexical_overlap; not factual confidence',
    'bm25': 'BM25 relevance; not factual confidence',
    'hybrid': 'RRF lexical+dense ranks; not factual confidence',
}


def term_counts(text):
    text = text.casefold()
    terms = re.findall(r'[a-z0-9_]+', text)
    for part in re.findall(r'[\u3040-\u30ff\u3400-\u9fff]+', text):
        terms.extend(part[i:i+2] for i in range(max(1, len(part)-1)))
    return Counter(terms)


def chunks(text):
    return [(start, min(start+400, len(text)), text[start:start+400])
            for start in range(0, len(text), 320)]


def bm25(query, texts):
    counts = [term_counts(text) for text in texts]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths)/max(1, len(lengths)) or 1
    terms = term_counts(query)
    frequencies = {term: sum(term in count for count in counts) for term in terms}
    scores = []
    for count, length in zip(counts, lengths):
        score = 0.0
        for term in terms:
            tf, df = count[term], frequencies[term]
            if tf:
                idf = math.log(1+(len(texts)-df+0.5)/(df+0.5))
                score += idf*tf*2.2/(tf+1.2*(0.25+0.75*length/average))
        scores.append(score)
    return scores


def checked_vectors(vectors, count, dimension):
    """The internal index has stable IDs, finite shape and normalized vectors."""
    try:
        if len(vectors) != count:
            raise ValueError()
        result = []
        for vector in vectors:
            if len(vector) != dimension:
                raise ValueError()
            values = array('f', vector)
            if not all(math.isfinite(value) for value in values):
                raise ValueError()
            norm = math.sqrt(sum(value*value for value in values))
            if abs(norm-1) > 0.001:
                raise ValueError()
            result.append(values)
        return result
    except (ValueError, TypeError, OverflowError) as error:
        raise APIError(409, 'retrieval_vector_integrity_failed') from error


class TextRanking:
    def __init__(self, encoder=None):
        self.encoder = encoder
        self.cache = OrderedDict()
        self.stats = {}

    def remove_asset(self, tenant, asset):
        for key in list(self.cache):
            if key[:2] == (tenant, asset):
                del self.cache[key]

    def rank(self, tenant, query, candidates, mode):
        started = time.perf_counter()
        chunked = []
        for parent, item in enumerate(candidates):
            # Titles are the first actual source line, never a filename/label.
            for start, end, text in chunks(item['segment']['quote']):
                chunked.append((parent, start, end, text))
        if len(chunked) > MAX_CHUNKS:
            raise APIError(413, 'text_retrieval_chunk_limit')
        texts = [part[3] for part in chunked]
        lexical = bm25(query, texts)
        def parents(scores, positive=False):
            maxima = {}
            for score, chunk in zip(scores, chunked):
                if positive and score <= 0:
                    continue
                maxima[chunk[0]] = max(score, maxima.get(chunk[0], -math.inf))
            return sorted(maxima.items(), key=lambda item: (-item[1], canonical(candidates[item[0]]['ref'])))
        lexical_rank = parents(lexical, positive=True)
        query_ms = document_ms = dense_ms = 0.0
        if mode == 'bm25':
            ranked = lexical_rank
        elif mode == 'hybrid':
            if self.encoder is None:
                raise APIError(503, 'text_embedding_model_unavailable')
            metadata = self.encoder.metadata
            dimension = metadata['dimension']
            version = digest(metadata)
            # Cache keys include tenant, asset, all evidence pins, raw hash,
            # exact text/title and encoder contract. Never reuse model versions.
            keys = [(tenant, candidates[p]['ref']['asset_id'], version,
                     digest(dict(evidence=candidates[p]['ref'],
                                 sha=candidates[p]['segment']['source_sha256'],
                                 title=candidates[p]['title'], start=start, end=end, text=text)))
                    for p, start, end, text in chunked]
            missing = [i for i, key in enumerate(keys) if key not in self.cache]
            t = time.perf_counter()
            if missing:
                document_inputs = [(candidates[chunked[i][0]]['title'], chunked[i][3]) for i in missing]
                vectors = checked_vectors(self.encoder.documents(document_inputs), len(missing), dimension)
                for i, vector in zip(missing, vectors):
                    self.cache[keys[i]] = vector
            document_ms = (time.perf_counter()-t)*1000
            try:
                vectors = checked_vectors([self.cache[key] for key in keys], len(keys), dimension)
            except APIError:
                for key in keys:
                    self.cache.pop(key, None)
                raise  # A broken index is rejected, never an unfiltered retry.
            t = time.perf_counter()
            vector = checked_vectors(self.encoder.query(query), 1, dimension)[0]
            query_ms = (time.perf_counter()-t)*1000
            t = time.perf_counter()
            dense = [sum(a*b for a,b in zip(vector, doc)) for doc in vectors]
            dense_rank = parents(dense)
            fused = Counter()
            for ranking in (lexical_rank[:20], dense_rank[:20]):
                for rank, (parent, _) in enumerate(ranking, 1):
                    fused[parent] += 1/(60+rank)
            ranked = sorted(fused.items(), key=lambda item: (-item[1], canonical(candidates[item[0]]['ref'])))
            dense_ms = (time.perf_counter()-t)*1000
            while len(self.cache) > MAX_CHUNKS:
                self.cache.popitem(last=False)
        else:
            raise APIError(422, 'unsupported_ranking_mode')
        self.stats = dict(eligible_segments=len(candidates), chunks=len(chunked),
                          query_encode_ms=query_ms, document_encode_ms=document_ms,
                          dense_and_fusion_ms=dense_ms, ranking_total_ms=(time.perf_counter()-started)*1000,
                          float_index_bytes=len(chunked)*((self.encoder.metadata['dimension']*4) if mode=='hybrid' else 0),
                          cached_float_bytes=sum(len(v)*v.itemsize for v in self.cache.values()),
                          metadata_and_python_overhead_in_index_bytes=False)
        return ranked
