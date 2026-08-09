# -*- coding: utf-8 -*-
"""
Anti-abuse guard for Oreus — protects the expensive endpoints (upload + process)
from bots and scripted flooding without getting in the way of real users.

Three independent layers:
  1. Bot user-agent filter  — cheap first pass, blocks obvious scripts.
  2. Per-IP sliding-window rate limit — the real defense against flooding.
  3. Per-IP concurrency cap + global queue cap — protects the CPU/Whisper slots.

All in-memory, no external dependency. State is small and self-pruning.
"""
import os, re, time, threading

_lock = threading.Lock()
_hits = {}   # ip -> {bucket: [timestamps]}

# Obvious non-browser clients. Real browsers always send a "Mozilla/..." UA.
# This is a weak layer (UA can be faked) — rate limiting below is the strong one.
_BOT_UA = re.compile(
    r'bot|crawl|spider|scrap|curl|wget|python-requests|httpie|http-client|'
    r'okhttp|go-http|java/|libwww|headless|phantom|puppeteer|playwright|axios|node-fetch',
    re.I,
)

def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default

# Windows per bucket: list of (max_hits, window_seconds).
# Defaults are generous for a real user (a handful of videos), tight for a flood.
_LIMITS = {
    'upload':  [(_int_env('OREUS_RL_UPLOAD_BURST', 8),  900),    # 8 / 15 min
                (_int_env('OREUS_RL_UPLOAD_DAY',   3),  86400)], # free tier : 3 / jour
    'process': [(_int_env('OREUS_RL_PROCESS_BURST', 8), 900),
                (_int_env('OREUS_RL_PROCESS_DAY',  3),  86400)], # free tier : 3 / jour
}

# Max active (queued + processing) jobs a single IP may hold at once.
PER_IP_ACTIVE_MAX = _int_env('OREUS_MAX_ACTIVE_PER_IP', 2)
# Max active jobs across the whole server before new ones are refused (503).
GLOBAL_ACTIVE_MAX = _int_env('OREUS_MAX_ACTIVE_GLOBAL', 12)


def is_bot_ua(ua):
    ua = (ua or '').strip()
    if not ua:                      # no UA at all → almost always a script
        return True
    return bool(_BOT_UA.search(ua))


def check_rate(ip, bucket):
    """Return (allowed, retry_after_seconds). Records the hit when allowed."""
    now = time.time()
    windows = _LIMITS.get(bucket)
    if not windows:
        return True, 0
    longest = max(w for _, w in windows)
    with _lock:
        per_ip = _hits.setdefault(ip, {})
        stamps = [t for t in per_ip.get(bucket, []) if now - t < longest]
        for max_hits, window in windows:
            recent = sum(1 for t in stamps if now - t < window)
            if recent >= max_hits:
                oldest = min(t for t in stamps if now - t < window)
                return False, int(window - (now - oldest)) + 1
        stamps.append(now)
        per_ip[bucket] = stamps
    return True, 0


def count_active_for_ip(jobs, ip):
    if not ip:
        return 0
    return sum(1 for j in jobs.values()
               if j.get('ip') == ip and j.get('status') in ('queued', 'processing'))


def count_active_global(jobs):
    return sum(1 for j in jobs.values()
               if j.get('status') in ('queued', 'processing'))


def prune(max_age=86400):
    """Drop stale IP entries so the table never grows unbounded."""
    now = time.time()
    with _lock:
        for ip in list(_hits.keys()):
            buckets = _hits[ip]
            for b in list(buckets.keys()):
                buckets[b] = [t for t in buckets[b] if now - t < max_age]
                if not buckets[b]:
                    del buckets[b]
            if not buckets:
                del _hits[ip]
