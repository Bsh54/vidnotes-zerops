"""Persistence layer for the Zerops deployment.

Two managed Zerops services back the app:
  - Postgres (service `db`): stores job records so results survive restarts.
  - Object storage (service `storage`): stores the generated files.

Both are optional. When their env vars are absent (local dev), every function
is a no-op and the app falls back to plain local-disk behaviour.
"""
import os
import json

# Postgres (values injected from the Zerops `db` service, see zerops.yaml)
_PGHOST = os.environ.get('PGHOST')
_PGPORT = os.environ.get('PGPORT', '5432')
_PGUSER = os.environ.get('PGUSER')
_PGPASSWORD = os.environ.get('PGPASSWORD')
_PGDATABASE = os.environ.get('PGDATABASE')

# Object storage (values injected from the Zerops `storage` service)
_S3_ENDPOINT = os.environ.get('S3_ENDPOINT')
_S3_KEY = os.environ.get('S3_ACCESS_KEY')
_S3_SECRET = os.environ.get('S3_SECRET_KEY')
_S3_BUCKET = os.environ.get('S3_BUCKET')

_SUFFIXES = ['_output.mp4', '_transcript.txt', '_summary.json', '_notes.pdf']


def db_enabled():
    return bool(_PGHOST and _PGUSER and _PGDATABASE)


def s3_enabled():
    return bool(_S3_ENDPOINT and _S3_KEY and _S3_SECRET and _S3_BUCKET)


# ── Object storage ──────────────────────────────────────────────────────────
def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client(
        's3', endpoint_url=_S3_ENDPOINT,
        aws_access_key_id=_S3_KEY, aws_secret_access_key=_S3_SECRET,
        region_name='us-east-1',
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}))


def upload_outputs(job_id, output_dir):
    # Push whatever output files exist for this job to object storage.
    if not s3_enabled():
        return
    try:
        c = _s3()
    except Exception as e:
        print(f'[s3] client: {e}', flush=True)
        return
    for suf in _SUFFIXES:
        p = os.path.join(str(output_dir), f'{job_id}{suf}')
        if os.path.exists(p):
            try:
                c.upload_file(p, _S3_BUCKET, f'{job_id}{suf}')
            except Exception as e:
                print(f'[s3] upload {suf}: {e}', flush=True)


def ensure_local(job_id, output_dir, suffix):
    # Return a local path for one output, fetching it from object storage if the
    # container no longer has it locally. Returns None if it is nowhere.
    p = os.path.join(str(output_dir), f'{job_id}{suffix}')
    if os.path.exists(p):
        return p
    if not s3_enabled():
        return None
    try:
        _s3().download_file(_S3_BUCKET, f'{job_id}{suffix}', p)
        return p
    except Exception:
        return None


# ── Postgres job records ────────────────────────────────────────────────────
def _conn():
    import pg8000.native as pg
    return pg.Connection(user=_PGUSER, password=_PGPASSWORD, host=_PGHOST,
                         port=int(_PGPORT), database=_PGDATABASE)


def init_db():
    if not db_enabled():
        return
    try:
        con = _conn()
        con.run('CREATE TABLE IF NOT EXISTS jobs '
                '(id TEXT PRIMARY KEY, data TEXT, updated TIMESTAMPTZ DEFAULT now())')
        con.close()
    except Exception as e:
        print(f'[db] init: {e}', flush=True)


def save_job(job_id, data):
    if not db_enabled():
        return
    try:
        con = _conn()
        con.run('INSERT INTO jobs (id, data) VALUES (:id, :d) '
                'ON CONFLICT (id) DO UPDATE SET data = :d, updated = now()',
                id=job_id, d=json.dumps(data))
        con.close()
    except Exception as e:
        print(f'[db] save {job_id}: {e}', flush=True)


def load_jobs():
    if not db_enabled():
        return {}
    out = {}
    try:
        con = _conn()
        rows = con.run('SELECT id, data FROM jobs ORDER BY updated DESC LIMIT 500')
        con.close()
        for jid, data in rows:
            try:
                out[jid] = json.loads(data)
            except Exception:
                pass
    except Exception as e:
        print(f'[db] load: {e}', flush=True)
    return out
