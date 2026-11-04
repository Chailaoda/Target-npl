"""Project only public text and already-anonymized author hashes from upstream stores."""
from datetime import datetime,timezone
from html.parser import HTMLParser
from .normalization import public_url


class PlainText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[]
    def handle_data(self,data):self.parts.append(data)


def timestamp(value):
    if isinstance(value,(int,float)) or str(value).isdigit():
        number=float(value)
        if number>10**11:number/=1000
        if 0<number<4102444800:return datetime.fromtimestamp(number,timezone.utc).isoformat()
    if isinstance(value,str):
        for parse in (lambda:datetime.fromisoformat(value),lambda:datetime.strptime(value,'%a %b %d %H:%M:%S %z %Y')):
            try:
                result=parse()
                if result.tzinfo:return result.astimezone(timezone.utc).isoformat()
            except ValueError:pass
    return ''


def project(platform,kind,item):
    comment=kind=='comment'
    cid=item.get('comment_id') if comment else item.get('note_id') or item.get('video_id') or item.get('content_id')
    root=(item.get('note_id') or item.get('video_id') or item.get('content_id') or '') if comment else ''
    text=item.get('content','') if comment else item.get('content_text') or item.get('content') or item.get('desc','')
    title=item.get('title','') if not comment else ''
    if title and title!=text:text=title+'\n'+(text or '')
    if platform in ('weibo','zhihu'):
        parser=PlainText();parser.feed(text or '');text=''.join(parser.parts)
    published=next((item[k] for k in ('create_time','time','created_time','publish_time') if item.get(k)),None)
    url=next((item[k] for k in ('note_url','video_url','content_url') if item.get(k)),'')
    if not cid or not isinstance(text,str):raise ValueError('Invalid source record')
    # Tokens, profile URLs, real IDs/nicknames, media URLs and arbitrary response fields are discarded.
    return {'content_id':str(cid),'text':text[:20000],'root_id':str(root),'parent_id':str(item.get('parent_comment_id') or ''),
        'author':'','creator_hash':item.get('creator_hash',''),'anonymized':True,
        'published_at':timestamp(published),'published_time_text':str(published or ''),
        'url':public_url(url,platform)}
