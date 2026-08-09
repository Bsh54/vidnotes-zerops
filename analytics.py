# -*- coding: utf-8 -*-
"""Petite couche d'analytics SQLite pour Oreus (visites + jobs)."""
import sqlite3, time, threading, datetime
from pathlib import Path
from urllib.parse import urlparse

DB = str(Path(__file__).parent / 'analytics.db')
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB, timeout=10)
    c.execute('PRAGMA journal_mode=WAL')
    return c


def init_db():
    with _lock, _conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS visits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, day TEXT, path TEXT, source TEXT, referrer TEXT,
            ip TEXT, ua TEXT, lang TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT, ts REAL, day TEXT, filename TEXT, size_mb REAL,
            src_lang TEXT, tgt_lang TEXT, sub_style TEXT, status TEXT,
            t_transcribe REAL, t_translate REAL, t_burn REAL, t_total REAL,
            n_segments INTEGER, error TEXT, ip TEXT)''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_visits_ts ON visits(ts)')
        c.execute('CREATE INDEX IF NOT EXISTS ix_jobs_ts ON jobs(ts)')


_SOURCES = [
    ('google', 'Google'), ('bing', 'Bing'), ('duckduckgo', 'DuckDuckGo'),
    ('yahoo', 'Yahoo'), ('ecosia', 'Ecosia'),
    ('facebook', 'Facebook'), ('fb.', 'Facebook'), ('instagram', 'Instagram'),
    ('t.co', 'Twitter/X'), ('twitter', 'Twitter/X'), ('x.com', 'Twitter/X'),
    ('youtube', 'YouTube'), ('youtu.be', 'YouTube'), ('tiktok', 'TikTok'),
    ('whatsapp', 'WhatsApp'), ('wa.me', 'WhatsApp'), ('telegram', 'Telegram'),
    ('t.me', 'Telegram'), ('linkedin', 'LinkedIn'), ('reddit', 'Reddit'),
    ('shadrakbessanh', 'Portfolio'),
]


def _src_from_ref(ref):
    if not ref:
        return 'Direct'
    try:
        h = urlparse(ref).netloc.lower()
    except Exception:
        return 'Autre'
    if not h:
        return 'Direct'
    if 'oreus.shadrakbessanh' in h:
        return 'Interne'
    for k, name in _SOURCES:
        if k in h:
            return name
    return h


def log_visit(path, referrer, ip, ua, lang):
    try:
        with _lock, _conn() as c:
            c.execute('INSERT INTO visits(ts,day,path,source,referrer,ip,ua,lang) '
                      'VALUES(?,?,?,?,?,?,?,?)',
                      (time.time(), time.strftime('%Y-%m-%d'), path,
                       _src_from_ref(referrer), (referrer or '')[:300],
                       ip or '', (ua or '')[:300], lang or ''))
    except Exception as e:
        print('[analytics] visit', e, flush=True)


def log_job(job_id='', filename='', size_mb=0, src_lang='', tgt_lang='', sub_style='',
            status='', t_transcribe=0, t_translate=0, t_burn=0, t_total=0,
            n_segments=0, error='', ip=''):
    try:
        with _lock, _conn() as c:
            c.execute('''INSERT INTO jobs(job_id,ts,day,filename,size_mb,src_lang,tgt_lang,
                sub_style,status,t_transcribe,t_translate,t_burn,t_total,n_segments,error,ip)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (job_id, time.time(), time.strftime('%Y-%m-%d'), filename, size_mb,
                 src_lang, tgt_lang, sub_style, status, t_transcribe, t_translate,
                 t_burn, t_total, n_segments, error, ip))
    except Exception as e:
        print('[analytics] job', e, flush=True)


def compute_stats():
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row

        def scalar(q, *a):
            r = c.execute(q, a).fetchone()
            return (r[0] if r and r[0] is not None else 0)

        total_visits = scalar('SELECT COUNT(*) FROM visits')
        unique_v = scalar('SELECT COUNT(DISTINCT ip) FROM visits')
        total_jobs = scalar('SELECT COUNT(*) FROM jobs')
        done = scalar("SELECT COUNT(*) FROM jobs WHERE status='done'")
        err = scalar("SELECT COUNT(*) FROM jobs WHERE status='error'")
        avg = c.execute("SELECT AVG(t_transcribe),AVG(t_translate),AVG(t_burn),AVG(t_total) "
                        "FROM jobs WHERE status='done'").fetchone()
        avg_size = scalar('SELECT AVG(size_mb) FROM jobs')

        days = [(datetime.date.today() - datetime.timedelta(days=i)).isoformat()
                for i in range(13, -1, -1)]
        vmap = dict(c.execute("SELECT day,COUNT(*) FROM visits GROUP BY day").fetchall())
        jmap = dict(c.execute("SELECT day,COUNT(*) FROM jobs GROUP BY day").fetchall())

        def lst(q):
            return [{'name': (r[0] or '?'), 'count': r[1]} for r in c.execute(q).fetchall()]

        sources = lst("SELECT source,COUNT(*) FROM visits GROUP BY source ORDER BY 2 DESC LIMIT 8")
        pages = lst("SELECT path,COUNT(*) FROM visits GROUP BY path ORDER BY 2 DESC LIMIT 8")
        langs = lst("SELECT tgt_lang,COUNT(*) FROM jobs GROUP BY tgt_lang ORDER BY 2 DESC LIMIT 12")
        styles = lst("SELECT sub_style,COUNT(*) FROM jobs GROUP BY sub_style ORDER BY 2 DESC LIMIT 8")

        recent_jobs = [dict(r) for r in c.execute(
            "SELECT ts,filename,src_lang,tgt_lang,sub_style,status,"
            "t_transcribe,t_translate,t_burn,t_total,size_mb,error,ip "
            "FROM jobs ORDER BY ts DESC LIMIT 40").fetchall()]
        recent_visits = [dict(r) for r in c.execute(
            "SELECT ts,path,source,referrer,ip,ua FROM visits "
            "ORDER BY ts DESC LIMIT 40").fetchall()]

    return {
        'totals': {'visits': total_visits, 'unique': unique_v, 'jobs': total_jobs,
                   'done': done, 'error': err, 'avg_size': round(avg_size or 0, 1)},
        'avg': {'transcribe': round(avg[0] or 0, 1), 'translate': round(avg[1] or 0, 1),
                'burn': round(avg[2] or 0, 1), 'total': round(avg[3] or 0, 1)},
        'visits_series': [{'day': d, 'count': vmap.get(d, 0)} for d in days],
        'jobs_series': [{'day': d, 'count': jmap.get(d, 0)} for d in days],
        'sources': sources, 'pages': pages, 'langs': langs, 'styles': styles,
        'recent_jobs': recent_jobs, 'recent_visits': recent_visits,
    }


def jobs_by_ids(ids):
    if not ids:
        return {}
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        q = ('SELECT job_id,filename,src_lang,tgt_lang,sub_style FROM jobs '
             'WHERE job_id IN (%s) ORDER BY ts ASC' % ','.join('?' * len(ids)))
        return {r['job_id']: dict(r) for r in c.execute(q, ids).fetchall()}
