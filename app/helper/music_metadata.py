"""Conservative optional MusicBrainz matching and Cover Art Archive retrieval.

No third-party Python dependencies. For personal-use installations, the
optional BMD_MUSICBRAINZ_CONTACT environment variable supplies a contact
URL/email in the User-Agent; set this before distributing the app.
"""
import difflib
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MUSICBRAINZ = 'https://musicbrainz.org/ws/2/recording/'
UUID = re.compile(r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$')
_LAST_REQUEST = 0.0
_THROTTLE = threading.Lock()
MAX_RESPONSE = 1_000_000
MAX_COVER = 6_000_000


def _normalize(s):
    s = unicodedata.normalize('NFKC', str(s or '')).casefold()
    s = re.sub(r'\s*\[({]\s*(official (music )?video|official audio|lyrics?|visualizer|hd|4k)\s*[\])}]', '', s)
    s = re.sub(r'[^\w ]+', ' ', s, flags=re.UNICODE)
    return ' '.join(s.split())


def _ratio(a, b):
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _credited_artist(entry):
    credit = entry.get('artist-credit') or []
    result = ''.join(x if isinstance(x, str) else x.get('name', '') for x in credit if isinstance(x, (str, dict)))
    return result.strip()


def _lucene(s):
    # Quoted Lucene phrase, with operators escaped; no unbounded user query.
    s = str(s or '')[:160]
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', s)


def _musicbrainz_request(url):
    global _LAST_REQUEST
    contact = os.environ.get('BMD_MUSICBRAINZ_CONTACT', '').strip()[:180]
    contact = contact if contact and ('@' in contact or contact.startswith('https://')) else 'personal local installation'
    headers = {'User-Agent': f'BraveMusicDownloader/2.5.0 ({contact})', 'Accept':'application/json'}
    with _THROTTLE:
        wait = 1.2 - (time.monotonic() - _LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST = time.monotonic()
        with build_opener(_HttpsOnly()).open(Request(url, headers=headers), timeout=8) as reply:
            if reply.status != 200:
                raise ValueError('MusicBrainz returned a non-200 status')
            data = reply.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise ValueError('MusicBrainz response too large')
            return json.loads(data)


class _HttpsOnly(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        host = (url.hostname or '').lower()
        allowed = host in ('musicbrainz.org','coverartarchive.org','archive.org') or host.endswith('.archive.org')
        if not allowed or url.port not in (None,443,80) or url.username or url.password:
            raise ValueError('Unexpected HTTP redirect target')
        if url.scheme == 'http' and (host == 'archive.org' or host.endswith('.archive.org')):
            # The CAA still documents some HTTP archive.org redirects. Upgrade,
            # never transmit artwork requests over plaintext HTTP.
            newurl = urlunsplit(('https',url.hostname,url.path,url.query,url.fragment))
        elif url.scheme != 'https':
            raise ValueError('Unexpected HTTP redirect scheme')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _best_release(recording, original_album):
    releases = [r for r in recording.get('releases',[]) if isinstance(r,dict) and UUID.fullmatch(str(r.get('id','')))]
    if not releases:
        return None
    def score(r):
        rg = r.get('release-group') or {}
        album = r.get('title','')
        return (
            4 if original_album and _ratio(album,original_album) >= .94 else 0,
            2 if rg.get('primary-type') == 'Album' else 0,
            1 if r.get('status') == 'Official' else 0,
            -(int((r.get('date') or '9999')[:4]) if str(r.get('date',''))[:4].isdigit() else 9999)
        )
    return max(releases, key=score)


def _cache_path(cache_dir, title, artist):
    key = hashlib.sha256((_normalize(title)+'|'+_normalize(artist)).encode('utf-8')).hexdigest()
    return Path(cache_dir) / ('mb-'+key+'.json')


def match_track(tags, cache_dir, request_json=None):
    """Returns (updated tags, match_info). Never applies low-confidence matches."""
    updated = dict(tags)
    title, artist = tags.get('title',''), tags.get('artist','')
    empty = {'status':'skipped','confidence':0,'recording_id':'','release_id':'','reason':'Not enough track information'}
    if not _normalize(title) or not _normalize(artist) or _normalize(artist) in ('unknown artist','various artists'):
        return updated, empty

    path = _cache_path(cache_dir,title,artist)
    payload = None
    try:
        if path.exists() and time.time()-path.stat().st_mtime < 30*86400:
            payload=json.loads(path.read_text(encoding='utf-8'))
        else:
            query = 'recording:"'+_lucene(title)+'" AND artist:"'+_lucene(artist)+'"'
            url = MUSICBRAINZ + '?' + urlencode({'query':query,'fmt':'json','limit':7})
            payload = (request_json or _musicbrainz_request)(url)
            if not isinstance(payload,dict) or not isinstance(payload.get('recordings'),list):
                raise ValueError('Unexpected MusicBrainz response')
            path.parent.mkdir(parents=True,exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
            tmp.replace(path)
    except (OSError, HTTPError, URLError, ValueError, TimeoutError, json.JSONDecodeError) as error:
        return updated, dict(empty,status='unavailable',reason=type(error).__name__)

    ranked = []
    for result in payload.get('recordings',[]):
        if not isinstance(result,dict) or not UUID.fullmatch(str(result.get('id',''))):
            continue
        candidate_artist = _credited_artist(result)
        title_sim = _ratio(title,result.get('title',''))
        artist_sim = _ratio(artist,candidate_artist)
        try: mb_score = max(0,min(1,float(result.get('score',0))/100))
        except (TypeError,ValueError): mb_score = 0
        confidence = .55*title_sim + .35*artist_sim + .10*mb_score
        if title_sim >= .92 and artist_sim >= .86 and confidence >= .92:
            ranked.append((confidence, result,candidate_artist))
    ranked.sort(key=lambda x:x[0],reverse=True)
    if not ranked or (len(ranked)>1 and ranked[0][0]-ranked[1][0]<.04 and ranked[0][1]['id']!=ranked[1][1]['id']):
        return updated, dict(empty,status='unmatched',reason='No unambiguous high-confidence recording match')

    confidence, best, artist_name = ranked[0]
    release = _best_release(best,tags.get('album',''))
    updated['title'] = best['title']
    updated['artist'] = artist_name
    if release:
        updated['album'] = release.get('title') or updated.get('album','')
        if release.get('date'):
            # Prefer a verified release date to a YouTube upload timestamp.
            # Keep the source value in JSON for traceability.
            updated['source_date'] = updated.get('date', '')
            updated['date'] = release['date'].replace('-','')
    updated['musicbrainz_recording_id'] = best['id']
    updated['musicbrainz_release_id'] = release['id'] if release else ''
    return updated, dict(status='matched',confidence=round(confidence,3),
                         recording_id=best['id'],release_id=updated['musicbrainz_release_id'],reason='High-confidence title/artist match')


def fetch_cover(release_id, target_file, fetch_bytes=None):
    """Fetch approved front art from the public CAA. Returns bool; no arbitrary URLs."""
    if not isinstance(release_id,str) or not UUID.fullmatch(release_id):
        return False
    url = f'https://coverartarchive.org/release/{release_id}/front-500'
    try:
        if fetch_bytes is None:
            req = Request(url, headers={'User-Agent':'BraveMusicDownloader/2.5.0 (personal local installation)'})
            with build_opener(_HttpsOnly()).open(req,timeout=10) as reply:
                data = reply.read(MAX_COVER+1)
        else:
            data = fetch_bytes(url)
        if not isinstance(data,bytes) or len(data)>MAX_COVER or len(data)<8 or not data.startswith(b'\xff\xd8\xff'):
            return False
        path=Path(target_file)
        path.write_bytes(data)
        return True
    except (OSError,HTTPError,URLError,TimeoutError,ValueError):
        return False
