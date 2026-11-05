"""Bounded, read-only run monitor. Uses committed analysis and original records."""
import json
import re
from collections import deque
from contextlib import closing


def _person_key(candidate):
    """Display grouping key for one candidate record.

    Never groups by nickname. Anonymous hashes are NOT globally unique - the
    same hash can occur on different platforms - so every key is namespaced
    by platform (A03). Records without any author key stay per-content.
    """
    platform = candidate.get('platform')
    author_key = candidate.get('author_key') or ""
    if author_key:
        return f"{platform}:{author_key}"
    return f"{platform}:content:{candidate.get('id')}"


def latest(service):
    files=list(service.runs.glob('*/status.json'))
    if not files:return None
    states=[json.loads(p.read_text(encoding='utf-8')) for p in files]
    state=max(states,key=lambda s:s.get('created_at',''))
    return service.status(state['run_id'])


def live(service,run_id):
    state=service.status(run_id)
    folder=service.runs/run_id
    dataset=state.get('dataset_id')
    pointer=folder/'collection-progress.json'
    if not dataset and pointer.exists():dataset=json.loads(pointer.read_text(encoding='utf-8')).get('dataset_id')
    captured=deque(maxlen=200);captured_total=0;collection={}
    sources=[]
    if dataset and re.fullmatch(r'\d{8}T\d{12}Z',dataset):
        source=service.root/'data/collections'/dataset
        checkpoint=source/'checkpoint.json'
        data=json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {}
        collection={k:data.get(k) for k in ('status','posts','comments','requests','active_platform','platform_statuses')}
        settings=data.get('settings') or {}
        if settings:collection['platforms']=settings.get('platforms') or [settings.get('platform')]
        if data.get('aggregate'):
            details=[]
            from .platforms import PLATFORMS
            for platform in PLATFORMS:
                child_pointer=folder/'platform-live'/platform/'collection-progress.json'
                if child_pointer.exists():
                    child_id=json.loads(child_pointer.read_text(encoding='utf-8')).get('dataset_id','')
                    if re.fullmatch(r'\d{8}T\d{12}Z',child_id):
                        child_source=service.root/'data/collections'/child_id;sources.append(child_source/'raw.jsonl')
                        child_checkpoint=child_source/'checkpoint.json'
                        if child_checkpoint.exists():
                            child=json.loads(child_checkpoint.read_text(encoding='utf-8'))
                            details.append({'platform':platform,'status':child.get('status'),'requests':child.get('requests',0)})
            if details:
                collection['platform_statuses']=details;collection['requests']=sum(p['requests'] for p in details)
        if not sources:sources=[source/'raw.jsonl']
    for raw in sources:
        if not raw.exists():continue
        # A trailing incomplete append is not a record until the next poll.
        for line in raw.read_text(encoding='utf-8').splitlines():
            try:row=json.loads(line)
            except ValueError:continue
            captured_total+=1
            item=row.get('data',row);kind=row.get('object_type','post');platform=row.get('platform','douyin')
            cid=item.get('comment_id',item.get('content_id','')) if kind=='comment' else item.get('aweme_id',item.get('content_id',''))
            text=item.get('content',item.get('text','')) if kind=='comment' else item.get('desc',item.get('title',item.get('text','')))
            published=item.get('published_at') or item.get('create_time') or ''
            if isinstance(published,(int,float)) or str(published).isdigit():
                from .platform_projection import timestamp
                published=timestamp(published)
            captured.append({'id':f'{platform}:{kind}:{cid}','platform':platform,'kind':kind,'text':str(text or '')[:2000],'keyword':row.get('keyword',''),'published_at':str(published or '')})
    job_id=state.get('analysis_job_id') or state.get('result',{}).get('job_id')
    candidates=[];processed=0;review=0
    db_path=service.root/'data/screening.sqlite3'
    if job_id and db_path.exists():
        import sqlite3
        from .workflow import projected_items
        with closing(sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            db.row_factory=sqlite3.Row
            job=db.execute('SELECT * FROM v2_jobs WHERE job_id=?',(job_id,)).fetchone()
            if job:
                for item in projected_items(db,job):
                    processed+=item['status'] in ('completed','needs_review','failed')
                    review+=item['status']=='needs_review'
                    result=item['result'] or {}
                    if result.get('accepted') and item['status']=='completed':
                        candidates.append({
                            'platform':item['source']['platform'],
                            'id':item['analysis_key'],
                            'text':item['source']['text'][:2000],
                            'reason':result.get('reason',''),
                            'evidence':result.get('evidence',{}),
                            'url':item['source'].get('url',''),
                            'author':item['source'].get('author') or item['source'].get('nickname',''),
                            'author_url':item['source'].get('author_url',''),
                            'author_key':item['source'].get('author_key',''),
                            'score':result.get('score',0),
                            'evidence_level':result.get('evidence_level','explicit'),
                            'quality_flags':result.get('quality_flags',[]),
                            'accepted':result.get('accepted',False),
                            'intent':result.get('intent','none'),
                            'stance':result.get('stance','unclear')
                        })
    person_exact = set()
    person_potential = set()
    for c in candidates:
        pkey = _person_key(c)
        if c.get('evidence_level') == 'explicit':
            person_exact.add(pkey)
        elif c.get('evidence_level') == 'related_only' or 'tier_potential' in c.get('quality_flags', []):
            person_potential.add(pkey)
    person_potential = person_potential - person_exact
    exact_count = len(person_exact)
    potential_count = len(person_potential)
    return {'run_id':run_id,'status':state['status'],'phase':state.get('phase'),
        'collection':collection,'captured':list(captured),'captured_total':captured_total,
        'candidates':candidates[-200:],'candidate_total':len(candidates),'exact_count':exact_count,'potential_count':potential_count,'processed':processed,'needs_review':review,
        'report_url':f'/reports/{run_id}' if state.get('result',{}).get('report') else None}
