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

    NOTE: this is a DISPLAY key, not the database account key. Public
    account keys already contain the platform prefix (e.g. "bilibili:42"),
    so grouping yields "bilibili:bilibili:42" - harmless for display, but
    WP5/WP6 must query account_repository by the ORIGINAL author_key, never
    by this grouped display key.
    """
    platform = candidate.get('platform')
    author_key = candidate.get('author_key') or ""
    if author_key:
        return f"{platform}:{author_key}"
    return f"{platform}:content:{candidate.get('id')}"


def _find_job_db(root, job_id):
    """Locate the database that actually holds this analysis job.

    The live monitor serves one store, and that store's database name is not
    guaranteed to equal the deployed one (tests and integration instances bind
    their own file). Prefer the deployment-declared name, then fall back to any
    SQLite file under data/ that already contains the job, so the viewer never
    reports zero progress against the wrong file.
    """
    import sqlite3
    from pathlib import Path
    from .db_access import resolve_db_for
    from .runtime_context import RuntimeErrorBase
    ordered = []
    try:
        ordered.append(resolve_db_for(root))
    except RuntimeErrorBase:
        pass
    data = Path(root) / "data"
    if data.is_dir():
        ordered += [p for p in sorted(data.glob("*.sqlite3")) if p not in ordered]
    for path in ordered:
        if not path.is_file():
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_jobs'").fetchone():
                    continue
                if db.execute("SELECT 1 FROM v2_jobs WHERE job_id=?", (job_id,)).fetchone():
                    return path
        except sqlite3.Error:
            continue
    return None


def latest(service):
    files=list(service.runs.glob('*/status.json'))
    if not files:return None
    states=[json.loads(p.read_text(encoding='utf-8')) for p in files]
    state=max(states,key=lambda s:s.get('created_at',''))
    return service.status(state['run_id'])


SEQ_SCALE = 1_000_000


def live(service,run_id,since_seq=None):
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
        collection={k:data.get(k) for k in ('status','posts','comments','requests','active_platform','platform_statuses','warnings','warning_count')}
        settings=data.get('settings') or {}
        if settings:collection['platforms']=settings.get('platforms') or [settings.get('platform')]
        if data.get('aggregate'):
            # Parent checkpoint entries are the ONLY authoritative final
            # verdict: each one was checked against the child's exit code and
            # receipt (non-zero exit + success receipt -> 'failed'). A child
            # checkpoint can still claim 'completed', so it may contribute
            # request counts / progress detail but must never replace that
            # verified status. Likewise, only verified-success lanes may feed
            # the live stream; a failed child's rows are never shown.
            from .platforms import PLATFORMS
            child_sources={}
            for platform in PLATFORMS:
                child_pointer=folder/'platform-live'/platform/'collection-progress.json'
                if not child_pointer.exists():continue
                child_id=json.loads(child_pointer.read_text(encoding='utf-8')).get('dataset_id','')
                if re.fullmatch(r'\d{8}T\d{12}Z',child_id):
                    child_sources[platform]=service.root/'data/collections'/child_id
            details=[];seen=set()
            for entry in (data.get('platform_statuses') or []):
                if not isinstance(entry,dict) or not entry.get('platform'):continue
                platform=entry['platform']
                if platform in seen:continue
                seen.add(platform)
                item=dict(entry)
                child_source=child_sources.get(platform)
                if child_source:
                    child_checkpoint=child_source/'checkpoint.json'
                    if child_checkpoint.exists():
                        child=json.loads(child_checkpoint.read_text(encoding='utf-8'))
                        # Detail only: never copy child['status'] onto item.
                        item.setdefault('requests',child.get('requests',0))
                    item.setdefault('dataset_id',child_source.name)
                    if item.get('status') in ('completed','completed_empty'):
                        sources.append(child_source/'raw.jsonl')
                details.append(item)
            if data.get('status')=='running':
                # Lanes the parent has not judged yet are still in flight.
                # Show live progress, but never an unverified terminal state.
                for platform in PLATFORMS:
                    if platform in seen or platform not in child_sources:continue
                    child_source=child_sources[platform]
                    child_checkpoint=child_source/'checkpoint.json'
                    child=json.loads(child_checkpoint.read_text(encoding='utf-8')) if child_checkpoint.exists() else {}
                    details.append({'platform':platform,'status':'running',
                        'requests':child.get('requests',0),'dataset_id':child_source.name})
                    sources.append(child_source/'raw.jsonl')
            if details:
                collection['platform_statuses']=details;collection['requests']=sum(p.get('requests',0) for p in details)
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
    db_path=_find_job_db(service.root,job_id) if job_id else None
    if db_path:
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
    rows=list(captured)
    total_candidates=len(candidates)
    # One cursor for two streams: high digits = rows read from the raw files,
    # low digits = accepted candidates. Both only ever move forward, so a
    # client can ask for "everything after N" without extra server state.
    seq=captured_total*SEQ_SCALE+total_candidates
    full={'run_id':run_id,'status':state['status'],'phase':state.get('phase'),
        'collection':collection,'captured':rows,'captured_total':captured_total,
        'candidates':candidates[-200:],'candidate_total':total_candidates,'exact_count':exact_count,'potential_count':potential_count,'processed':processed,'needs_review':review,
        'report_url':f'/reports/{run_id}' if state.get('result',{}).get('report') else None,
        'next_seq':seq,'incremental':False}
    if since_seq is None:
        return full
    try:
        since=int(since_seq)
    except (TypeError,ValueError) as error:
        raise ValueError('since_seq 必须是非负整数') from error
    if since<0:
        raise ValueError('since_seq 必须是非负整数')
    if since>seq:
        return full
    previous_rows,previous_candidates=divmod(since,SEQ_SCALE)
    offset=max(0,captured_total-len(rows))
    if previous_rows<offset or previous_candidates>total_candidates:
        # The requested slice has already scrolled out of the bounded window:
        # resync with a full payload instead of silently skipping records.
        return full
    return {'run_id':run_id,'status':state['status'],'phase':state.get('phase'),
        'collection':collection,'incremental':True,'next_seq':seq,
        'captured_delta':rows[previous_rows-offset:],
        'candidates_delta':candidates[previous_candidates:],
        'captured_total':captured_total,'candidate_total':total_candidates,
        'exact_count':exact_count,'potential_count':potential_count,
        'processed':processed,'needs_review':review,
        'report_url':full['report_url']}
