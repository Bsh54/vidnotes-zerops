import os, subprocess, json, shutil, requests, threading
import analytics
from pathlib import Path

# Traduction : Google Gemini (remplace Lewis). Clé en variable d'env GEMINI_API_KEY.
# Modèle 'gemini-flash-lite-latest' = rapide (~3-4s) et excellente qualité FR/EN/argot.
GEMINI_KEY   = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-flash-lite-latest')
GEMINI_URL   = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'

AFRICAN_LANGS = {
    'sw','yo','ha','ig','am','zu','xh','sn','ny','st','rw','lg','ln','ee',
    'tw','krio','nso','ts','tn','ti','om','mg','so','af','bm','wo','fon',
    'kg','ss','ve','gaa','ak','aa','bem','run','cgg','mfe','nus','luo',
}


TRANSLATE_API_URL = 'https://shadsai2api-cloudflare.shadobsh.workers.dev/v1/chat/completions'
TRANSLATE_API_KEY = '1'
TRANSLATE_HEADERS = {
    'Content-Type': 'application/json',
    'Authorization': 'Bearer 1',
    'User-Agent': 'python-requests/2.31.0',
}


def _google_translate_text(text, tgt_lang):
    # Un appel Google Translate, renvoie le contenu brut (ou leve).
    resp = requests.post(
        TRANSLATE_API_URL,
        json={
            'model': 'google-translate',
            'messages': [{'role': 'user', 'content': text}],
            'source_lang': 'auto',
            'target_lang': tgt_lang,
            'stream': False,
        },
        headers=TRANSLATE_HEADERS,
        timeout=25,
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']


def _translate_google(segments, tgt_lang):
    # Langues africaines via Google. Traduit par PARAGRAPHE (plusieurs phrases
    # d'un coup -> Google a le contexte des phrases voisines) au lieu de ligne
    # par ligne. Repli surete ligne-a-ligne si le compte de lignes ne colle pas.
    if not segments:
        return segments
    results = []
    batch = 8
    SEP = '\n'
    for i in range(0, len(segments), batch):
        grp = segments[i:i + batch]
        texts = [s['text'].strip() for s in grp]
        # Tentative paragraphe : seulement si toutes les lignes sont non vides
        if all(texts):
            try:
                out = _google_translate_text(SEP.join(texts), tgt_lang)
                parts = [p.strip() for p in out.split('\n')]
                if len(parts) == len(texts) and all(parts):
                    for seg, tr in zip(grp, parts):
                        results.append({'start': seg['start'], 'end': seg['end'], 'text': tr})
                    continue
            except Exception as e:
                print(f'[translate_google] bloc {i} repli: {e}', flush=True)
        # Repli : ligne par ligne (comportement d'origine, teste et fiable)
        for seg in grp:
            t = seg['text'].strip()
            if not t:
                results.append(seg)
                continue
            try:
                tr = _google_translate_text(t, tgt_lang).strip()
                tr = tr.replace('—',' ').replace('–',' ').replace('--',' ')
                tr = ' '.join(tr.split())
                results.append({'start': seg['start'], 'end': seg['end'], 'text': tr})
            except Exception as e:
                print(f'[translate_google] erreur ligne: {e}', flush=True)
                results.append(seg)
    return results



# ── Whisper singleton — chargé une seule fois au démarrage ────────────────────
# Modèle configurable. Défaut 'small' = léger (~1 Go RAM) et rapide, pour tenir
# sur de l'hébergement gratuit. Mettre OREUS_WHISPER_MODEL=medium pour + de fidélité.
_WHISPER_MODEL = os.environ.get('OREUS_WHISPER_MODEL', 'small')
# Isolation de voix Demucs : DÉSACTIVÉE par défaut (trop lourde RAM/CPU). On garde
# le débruitage ffmpeg léger. Mettre OREUS_USE_DEMUCS=1 pour la réactiver.
_USE_DEMUCS = os.environ.get('OREUS_USE_DEMUCS', '0') != '0'

_model = None
_model_lock = threading.Lock()

def get_whisper():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel
                print(f'[whisper] Chargement du modèle {_WHISPER_MODEL}...', flush=True)
                _model = WhisperModel(_WHISPER_MODEL, device='cpu', compute_type='int8', cpu_threads=4)
                print('[whisper] Modèle prêt.', flush=True)
    return _model

# Pré-charger en arrière-plan au démarrage — premier job immédiat.
# Désactivé par défaut : en prod (Zerops) on transcrit via Groq, donc faster-whisper
# n'a pas besoin d'être installé. Mettre VIDNOTES_LOCAL_WHISPER=1 pour l'activer.
if os.environ.get('VIDNOTES_LOCAL_WHISPER') == '1':
    threading.Thread(target=lambda: get_whisper(), daemon=True).start()

# ── Sémaphore — max 2 traitements simultanés, les autres attendent ─────────────
_sem = threading.Semaphore(2)
_queue_lock = threading.Lock()
_queue_count = 0


def _set(jobs, lock, job_id, **kw):
    with lock:
        jobs[job_id].update(kw)
        if kw.get('status') in ('done', 'error'):
            try:
                p = Path('jobs') / f'{job_id}.json'
                with open(p, 'w') as f:
                    json.dump(jobs[job_id], f)
            except Exception:
                pass


def process_video(job_id, jobs, lock, output_dir):
    global _queue_count
    output_dir = Path(output_dir)

    # Marquer en file d'attente
    with _queue_lock:
        _queue_count += 1
        pos = _queue_count
    _set(jobs, lock, job_id, status='queued', progress=0, step=0, queue_pos=pos)

    # Attendre un slot libre (bloquant mais dans un thread séparé)
    _sem.acquire()
    with _queue_lock:
        _queue_count -= 1

    try:
        _run(job_id, jobs, lock, output_dir)
    finally:
        _sem.release()


class _Abandoned(Exception):
    """La page a été fermée : plus aucun heartbeat depuis trop longtemps."""


# Sans nouvelle du navigateur pendant ce délai, on abandonne le job pour
# libérer le CPU. 150 s = tolérant aux réseaux instables/écrans verrouillés.
_ABANDON_AFTER = int(os.environ.get('VIDNOTES_ABANDON_AFTER', 150))


def _run(job_id, jobs, lock, output_dir):
    import time as _tt

    def _check_alive():
        with lock:
            seen = jobs.get(job_id, {}).get('last_seen')
        if seen and (_tt.time() - seen) > _ABANDON_AFTER:
            raise _Abandoned()

    with lock:
        j = dict(jobs[job_id])
    filepath = j['filepath']
    src_lang = j.get('src_lang', 'auto')
    tgt_lang = j.get('tgt_lang', 'en')

    # Server-limit: only the first 15 minutes are processed (matches the UI notice).
    MAX_SECONDS = int(os.environ.get('OREUS_MAX_SECONDS', '900'))
    try:
        _dur = _video_duration(filepath)
        if _dur and _dur > MAX_SECONDS + 5:
            _trimmed = filepath.rsplit('.', 1)[0] + '_cap.mp4'
            _r = subprocess.run(['ffmpeg', '-y', '-i', filepath, '-t', str(MAX_SECONDS), '-c', 'copy', _trimmed], capture_output=True, text=True)
            if _r.returncode != 0 or not os.path.exists(_trimmed) or os.path.getsize(_trimmed) == 0:
                subprocess.run(['ffmpeg', '-y', '-i', filepath, '-t', str(MAX_SECONDS), _trimmed], capture_output=True, text=True)
            if os.path.exists(_trimmed) and os.path.getsize(_trimmed) > 0:
                try:
                    os.remove(filepath)
                except Exception:
                    pass
                filepath = _trimmed
                with lock:
                    jobs[job_id]['filepath'] = filepath
                print(f'[worker] capped to first {MAX_SECONDS}s (source was {_dur:.0f}s)', flush=True)
    except Exception as _e:
        print('[worker] duration cap skipped:', _e, flush=True)

    import time as _t
    size_mb = 0.0
    try:
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
    except Exception:
        pass
    t_tr = t_tl = t_bn = 0.0
    n_seg = 0

    def _record(status, error=''):
        try:
            analytics.log_job(
                job_id=job_id, filename=j.get('filename', ''), size_mb=round(size_mb, 2),
                src_lang=src_lang, tgt_lang=tgt_lang, sub_style=j.get('sub_style', ''),
                status=status, t_transcribe=round(t_tr, 1), t_translate=round(t_tl, 1),
                t_burn=round(t_bn, 1), t_total=round(t_tr + t_tl + t_bn, 1),
                n_segments=n_seg, error=str(error)[:300], ip=j.get('ip', ''))
        except Exception as _e:
            print('[worker] analytics:', _e, flush=True)

    try:
        # ── Mode embellissement seul (service distinct, sans sous-titres) ──
        # On saute transcription + traduction : on embellit juste la vidéo.
        if j.get('mode') == 'enhance':
            _set(jobs, lock, job_id, status='processing', progress=10, step=1)
            out_path = str(output_dir / f'{job_id}_output.mp4')
            _s = _t.time()
            _enhance_only(filepath, out_path)
            t_bn = _t.time() - _s
            _set(jobs, lock, job_id, progress=100, step=1, status='done', output=out_path)
            _record('done')
            try:
                os.remove(filepath)
            except Exception:
                pass
            return

        # ── Step 1 : Transcription ─────────────────────────────────
        _set(jobs, lock, job_id, status='processing', progress=5, step=1)
        _s = _t.time()
        _last = [0]
        def _tr_prog(frac):
            _check_alive()   # checkpoint fin : à chaque segment transcrit
            p = 5 + int(28 * frac)
            if p > _last[0]:
                _last[0] = p
                _set(jobs, lock, job_id, progress=p, step=1)
        segments = _transcribe(filepath, src_lang, on_progress=_tr_prog)
        t_tr = _t.time() - _s
        n_seg = len(segments)
        _set(jobs, lock, job_id, progress=35, step=1)

        # ── Step 2 : Traduction ────────────────────────────────────
        _check_alive()
        _set(jobs, lock, job_id, progress=38, step=2)
        _s = _t.time()
        if tgt_lang in AFRICAN_LANGS:
            translated = _translate_google(segments, tgt_lang)
        else:
            translated = _translate(segments, tgt_lang)
        t_tl = _t.time() - _s
        _set(jobs, lock, job_id, progress=72, step=2)

        # ── Step 2b : Résumé de cours ultra-détaillé + transcription ──
        # Généré à partir de la transcription originale, rédigé dans la langue
        # cible. Non bloquant : un échec du résumé ne casse pas la vidéo.
        _check_alive()
        try:
            _write_transcript(translated, str(output_dir / f'{job_id}_transcript.txt'))
            summary_md = _study_summary(segments, tgt_lang)
            with open(str(output_dir / f'{job_id}_summary.json'), 'w', encoding='utf-8') as sf:
                json.dump({'language': tgt_lang, 'markdown': summary_md}, sf, ensure_ascii=False)
        except Exception as e:
            print(f'[summary] indisponible: {e}', flush=True)

        # ── Step 3 : Incrustation ──────────────────────────────────
        _check_alive()
        _set(jobs, lock, job_id, progress=75, step=3)
        srt_path = str(output_dir / f'{job_id}.srt')
        out_path = str(output_dir / f'{job_id}_output.mp4')
        _write_srt(translated, srt_path)
        _SIZE_SCALE = {'small': 0.82, 'medium': 1.0, 'large': 1.28}
        _sz = j.get('sub_size', 'medium')
        try:
            _scale = float(_sz)
        except (TypeError, ValueError):
            _scale = _SIZE_SCALE.get(_sz, 1.0)
        _scale = max(0.6, min(1.8, _scale))
        _s = _t.time()
        _burn(filepath, srt_path, out_path, j.get('sub_style','classic'),
              _scale, j.get('sub_color') or None, enhance=bool(j.get('enhance')))
        t_bn = _t.time() - _s
        _set(jobs, lock, job_id, progress=100, step=3, status='done', output=out_path)
        _record('done')

        try:
            os.remove(filepath)
        except Exception:
            pass

    except _Abandoned:
        _set(jobs, lock, job_id, status='error',
             error='Cancelled: the page was closed during processing.')
        print(f'[worker] job={job_id} abandonne (page fermee), CPU libere', flush=True)
        _record('abandoned')
        try:
            os.remove(filepath)
        except Exception:
            pass
    except Exception as exc:
        _set(jobs, lock, job_id, status='error', error=str(exc))
        print(f'[worker] job={job_id} error: {exc}', flush=True)
        _record('error', exc)


# Demucs est lourd (PyTorch + ~RAM) : on n'en lance qu'un seul à la fois,
# même si deux jobs tournent en parallèle, pour ne pas saturer la VM.
_demucs_sem = threading.Semaphore(1)


def _extract_full_audio(src, dst):
    # Audio plein spectre (44.1 kHz stéréo) — ce que Demucs attend pour bien
    # séparer la voix de la musique. Lève si ffmpeg échoue.
    cmd = ['ffmpeg', '-y', '-i', src, '-vn', '-ar', '44100', '-ac', '2', dst]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError(f'extract audio: {r.stderr[-300:]}')


def _isolate_vocals(audio_path, workdir):
    # Sépare la piste voix avec Demucs (htdemucs, --two-stems=vocals).
    # Renvoie le chemin du vocals.wav, ou None si indisponible/échec (repli).
    try:
        import demucs  # noqa: F401
    except Exception:
        return None
    import sys as _sys
    out_root = os.path.join(workdir, 'demucs')
    with _demucs_sem:
        cmd = [
            _sys.executable, '-m', 'demucs',
            '--two-stems=vocals', '-n', 'htdemucs',
            '--segment', '7',             # limite la RAM (max ~7.8s pour htdemucs)
            '--mp3',                      # encodage via lameenc (torchaudio.save exige torchcodec)
            '-o', out_root, audio_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        print(f'[demucs] échec, repli sans séparation: {r.stderr[-300:]}', flush=True)
        return None
    stem = Path(audio_path).stem
    vocals = os.path.join(out_root, 'htdemucs', stem, 'vocals.mp3')
    return vocals if os.path.exists(vocals) else None


def _clean_voice(src, dst):
    # Bande de la voix humaine + débruitage FFT, ramené en 16 kHz mono
    # (entrée idéale pour Whisper). Lève si ffmpeg échoue.
    cmd = ['ffmpeg', '-y', '-i', src, '-vn',
           '-af', 'highpass=f=120,lowpass=f=8000,afftdn=nf=-25',
           '-ar', '16000', '-ac', '1', dst]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError(f'clean voice: {r.stderr[-300:]}')


def _prepare_audio(filepath):
    # Construit l'audio le plus propre possible pour la transcription :
    # extraction -> (Demucs voix) -> nettoyage/débruitage 16k mono.
    # Renvoie (audio_path, workdir_a_nettoyer). En cas d'échec total, renvoie
    # le fichier original pour ne jamais bloquer un job.
    import tempfile, uuid as _u
    workdir = tempfile.mkdtemp(prefix='oreus_aud_')
    try:
        full = os.path.join(workdir, f'full_{_u.uuid4().hex[:8]}.wav')
        _extract_full_audio(filepath, full)

        voice_src = full
        if _USE_DEMUCS:
            vocals = _isolate_vocals(full, workdir)
            if vocals:
                voice_src = vocals
                print('[audio] voix isolée (Demucs)', flush=True)

        clean = os.path.join(workdir, f'clean_{_u.uuid4().hex[:8]}.wav')
        _clean_voice(voice_src, clean)
        return clean, workdir
    except Exception as e:
        print(f'[audio] préparation indisponible, audio brut: {e}', flush=True)
        return filepath, workdir


def _video_duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


GROQ_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_STT_MODEL = os.environ.get('GROQ_STT_MODEL', 'whisper-large-v3-turbo')


def _transcribe_groq(filepath, src_lang, duration):
    # Transcription via l'API Groq (Whisper large-v3-turbo sur GPU) :
    # quelques secondes au lieu de plusieurs minutes, et meilleure precision
    # que le modele local. Leve en cas d'echec (repli local ensuite).
    import uuid as _u, tempfile
    if not GROQ_KEY:
        raise RuntimeError('GROQ_API_KEY absente')
    workdir = tempfile.mkdtemp(prefix='vidnotes_groq_')
    try:
        # Audio compact mono 16k mp3 (limite Groq free : 25 Mo / ~2h en 32kbps)
        mp3 = os.path.join(workdir, f'a_{_u.uuid4().hex[:8]}.mp3')
        r = subprocess.run(['ffmpeg', '-y', '-i', filepath, '-vn', '-ac', '1',
                            '-ar', '16000', '-b:a', '32k', mp3],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(mp3):
            raise RuntimeError(f'extract mp3: {r.stderr[-200:]}')
        if os.path.getsize(mp3) > 24 * 1024 * 1024:
            raise RuntimeError('audio trop long pour l API Groq')
        data = {'model': GROQ_STT_MODEL, 'response_format': 'verbose_json'}
        if src_lang != 'auto':
            data['language'] = src_lang
        with open(mp3, 'rb') as fh:
            resp = requests.post(
                'https://api.groq.com/openai/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {GROQ_KEY}'},
                files={'file': ('audio.mp3', fh, 'audio/mpeg')},
                data=data, timeout=300)
        resp.raise_for_status()
        segs = resp.json().get('segments') or []
        out = [{'start': float(s['start']), 'end': float(s['end']),
                'text': str(s['text']).strip()}
               for s in segs if str(s.get('text', '')).strip()]
        if not out:
            raise ValueError('aucun segment')
        print(f'[groq-stt] ok: {len(out)} segments', flush=True)
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _transcribe_gemini(filepath, src_lang, duration):
    # Transcription via l'API Gemini (audio écouté sur les serveurs Google) :
    # ~20-30 s au lieu de plusieurs minutes en local. Lève en cas de problème
    # (le repli Whisper local prend alors le relais).
    import base64, uuid as _u, tempfile
    if not GEMINI_KEY:
        raise RuntimeError('GEMINI_API_KEY absente')
    workdir = tempfile.mkdtemp(prefix='vidnotes_gem_')
    try:
        # Audio compact : mono 16k, mp3 32 kbps -> ~7 Mo pour 30 min (limite inline 20 Mo)
        mp3 = os.path.join(workdir, f'a_{_u.uuid4().hex[:8]}.mp3')
        r = subprocess.run(['ffmpeg', '-y', '-i', filepath, '-vn', '-ac', '1',
                            '-ar', '16000', '-b:a', '32k', mp3],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(mp3):
            raise RuntimeError(f'extract mp3: {r.stderr[-200:]}')
        if os.path.getsize(mp3) > 19 * 1024 * 1024:
            raise RuntimeError('audio trop long pour l inline API')
        b64 = base64.b64encode(open(mp3, 'rb').read()).decode()

        lang_hint = '' if src_lang == 'auto' else f' The speech is in language code "{src_lang}".'
        prompt = (
            'Transcribe this audio precisely, word for word, in its ORIGINAL language.'
            + lang_hint +
            f' Total duration: {duration:.0f} seconds. '
            'Return ONLY a valid JSON array of segments, each {"start": seconds, "end": seconds, "text": "..."}. '
            'Segments must be short (max ~7 seconds / ~15 words), in chronological order, '
            'with timestamps as accurate as possible. No markdown, no explanation.'
        )
        body = {
            'contents': [{'parts': [
                {'inline_data': {'mime_type': 'audio/mp3', 'data': b64}},
                {'text': prompt},
            ]}],
            'generationConfig': {'temperature': 0.0, 'maxOutputTokens': 65536},
            'safetySettings': _GEMINI_SAFETY,
        }
        headers = {'Content-Type': 'application/json', 'X-goog-api-key': GEMINI_KEY}
        resp = requests.post(GEMINI_URL, json=body, headers=headers, timeout=300)
        resp.raise_for_status()
        cands = resp.json().get('candidates') or []
        if not cands:
            raise ValueError('Gemini: reponse vide')
        content = ''.join(p.get('text', '') for p in
                          (cands[0].get('content') or {}).get('parts') or [])
        segs = json.loads(_strip_fences(content))
        if not isinstance(segs, list) or not segs:
            raise ValueError('JSON segments invalide')
        out = []
        for s in segs:
            try:
                st = float(s['start'])
                en = float(s['end'])
                tx = str(s.get('text') or s.get('transcript') or '').strip()
            except (KeyError, TypeError, ValueError):
                continue
            if tx and en > st:
                out.append({'start': st, 'end': en, 'text': tx})
        if not out:
            raise ValueError('aucun segment exploitable')
        # Sanity : la couverture doit ressembler a la duree reelle
        if duration > 60 and out[-1]['end'] < duration * 0.5:
            raise ValueError('couverture partielle, repli local')
        print(f'[gemini-stt] ok: {len(out)} segments', flush=True)
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _transcribe(filepath, src_lang, on_progress=None):
    # Voie rapide : Groq API (Whisper large GPU). Repli : Whisper local.
    duration = _video_duration(filepath)
    try:
        if on_progress:
            on_progress(0.15)   # signale que ca travaille
        return _transcribe_groq(filepath, src_lang, duration)
    except Exception as e:
        print(f'[groq-stt] indisponible ({e})', flush=True)
        if os.environ.get('VIDNOTES_LOCAL_WHISPER') != '1':
            raise
        print('[groq-stt] repli Whisper local', flush=True)
    return _transcribe_local(filepath, src_lang, on_progress)


def _transcribe_local(filepath, src_lang, on_progress=None):
    model = get_whisper()
    lang = None if src_lang == 'auto' else src_lang
    duration = _video_duration(filepath)
    audio_path, workdir = _prepare_audio(filepath)
    try:
        segs, _ = model.transcribe(
            audio_path,
            language=lang,
            beam_size=2,
            vad_filter=True,                                  # coupe musique/bruit sans voix
            vad_parameters=dict(min_silence_duration_ms=500),
            condition_on_previous_text=False,                 # stoppe les boucles d'hallucination
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],       # repli progressif si peu sûr
        )
        out = []
        for s in segs:
            out.append({'start': s.start, 'end': s.end, 'text': s.text.strip()})
            # segs est un générateur : la progression avance au fil de l'écoute.
            if on_progress and duration > 0:
                on_progress(min(1.0, s.end / duration))
        return out
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


_GEMINI_SAFETY = [
    {'category': 'HARM_CATEGORY_HARASSMENT',        'threshold': 'BLOCK_NONE'},
    {'category': 'HARM_CATEGORY_HATE_SPEECH',       'threshold': 'BLOCK_NONE'},
    {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_NONE'},
    {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_NONE'},
]


def _ds_chat(system, user, timeout=90):
    # Un appel Gemini, renvoie le contenu texte (ou leve). Retry court sur
    # surcharge (503/429). safetySettings BLOCK_NONE pour ne pas bloquer un
    # sous-titre legitime (argot, sante, argent, etc.).
    import time as _time
    body = {
        'system_instruction': {'parts': [{'text': system}]},
        'contents': [{'parts': [{'text': user}]}],
        'generationConfig': {'temperature': 0.3},
        'safetySettings': _GEMINI_SAFETY,
    }
    headers = {'Content-Type': 'application/json', 'X-goog-api-key': GEMINI_KEY}
    last = ''
    for attempt in range(3):
        resp = requests.post(GEMINI_URL, json=body, headers=headers, timeout=timeout)
        if resp.status_code in (429, 503):
            last = str(resp.status_code)
            _time.sleep(1.5 * (attempt + 1))
            continue
        resp.raise_for_status()
        cands = resp.json().get('candidates') or []
        if not cands:
            raise ValueError('Gemini: aucune reponse (candidates vide)')
        parts = (cands[0].get('content') or {}).get('parts') or []
        return ''.join(p.get('text', '') for p in parts).strip()
    raise RuntimeError(f'Gemini surcharge ({last})')


def _strip_fences(content):
    # Retire les ``` et le prefixe json eventuels autour d'une reponse.
    content = content.strip()
    if '```' in content:
        parts = content.split('```')
        content = parts[1] if len(parts) > 1 else content
        if content.lower().startswith('json'):
            content = content[4:]
    return content.strip()


def _build_brief(segments, tgt_lang):
    # Phase 1 (comprehension) : l'IA lit TOUTE la transcription et produit un
    # resume + un glossaire des termes/noms recurrents, pour que la traduction
    # qui suit reste coherente du debut a la fin. Renvoie '' si indisponible.
    full = ' '.join(s['text'].strip() for s in segments if s['text'].strip())
    if not full:
        return ''
    full = full[:8000]
    try:
        sys_p = ('You analyze a video transcript before it is translated, '
                 'to help a subtitle translator stay consistent.')
        usr_p = (
            'Read this full transcript and understand its topic, tone and recurring terms. '
            f'It will be translated into language code "{tgt_lang}". '
            'Return ONLY a compact JSON object: '
            '{"summary": "one short sentence on the topic and tone", '
            '"glossary": {"name or recurring term": "how to render it consistently in the target language"}}. '
            'Keep the glossary to the few most important recurring names/terms only. '
            'No markdown, no explanation.\n\nTRANSCRIPT:\n' + full
        )
        data = json.loads(_strip_fences(_ds_chat(sys_p, usr_p, timeout=60)))
        summary = str(data.get('summary', '')).strip()
        glossary = data.get('glossary', {}) or {}
        lines = []
        if summary:
            lines.append('CONTEXT: ' + summary)
        if isinstance(glossary, dict) and glossary:
            gl = '; '.join(f'{k} -> {v}' for k, v in list(glossary.items())[:20])
            lines.append('GLOSSARY (use these consistently): ' + gl)
        brief = '\n'.join(lines)
        if brief:
            print(f'[translate] brief ok ({len(glossary)} termes)', flush=True)
        return brief
    except Exception as e:
        print(f'[translate] brief indisponible: {e}', flush=True)
        return ''


def _translate_chunk_lines(texts, tgt_lang, brief, prev_pairs):
    # Traduit une liste de lignes en gardant le contexte global (brief) et local
    # (prev_pairs = dernieres lignes deja traduites). Renvoie une liste de meme
    # longueur, ou leve si la sortie est incoherente (declenche le repli).
    payload = json.dumps(texts, ensure_ascii=False)
    ctx = ''
    if brief:
        ctx += brief + '\n\n'
    if prev_pairs:
        prev = '\n'.join(f'- {a}  =>  {b}' for a, b in prev_pairs)
        ctx += 'PREVIOUS LINES (context for continuity, do NOT re-translate them):\n' + prev + '\n\n'
    prompt = (
        ctx +
        f'Translate this JSON array of subtitle lines into language code "{tgt_lang}". '
        'First read them as one continuous flow, then translate naturally and coherently, '
        'keeping pronouns, terminology and tone consistent with the context above. '
        'Return ONLY a valid JSON array of EXACTLY the same length and same order, '
        'one translated string per input line. No explanation, no markdown.\n\n' + payload
    )
    content = _strip_fences(_ds_chat(
        'You are a professional subtitle translator. You preserve meaning and coherence '
        'across the whole video. Output only a valid JSON array, same length as the input.',
        prompt, timeout=90))
    out = json.loads(content)
    if not isinstance(out, list) or len(out) != len(texts):
        got = len(out) if isinstance(out, list) else '?'
        raise ValueError(f'longueur {got} != {len(texts)}')
    return [str(x) for x in out]


def _translate(segments, tgt_lang):
    # Langues standard via DeepSeek. Comprehension globale d'abord (brief), puis
    # traduction par blocs de 30 lignes avec continuite locale entre les blocs.
    if not segments:
        return segments
    brief = _build_brief(segments, tgt_lang)
    texts = [s['text'] for s in segments]
    results = []
    chunk = 30
    for i in range(0, len(texts), chunk):
        block = texts[i:i + chunk]
        prev_pairs = []
        if results:
            k = min(3, len(results))
            prev_pairs = list(zip(texts[i - k:i], results[-k:]))
        try:
            translated = _translate_chunk_lines(block, tgt_lang, brief, prev_pairs)
        except Exception as e:
            print(f'[translate] bloc {i} repli ligne-a-ligne: {e}', flush=True)
            translated = []
            for t in block:
                try:
                    translated.append(_translate_chunk_lines([t], tgt_lang, brief, [])[0])
                except Exception:
                    translated.append(t)
        results.extend(translated)
    def _clean(t):
        t = t.replace('—', ' ').replace('–', ' ').replace(' -- ', ' ').replace('--', ' ')
        return ' '.join(t.split())
    return [{'start': s['start'], 'end': s['end'], 'text': _clean(t)}
            for s, t in zip(segments, results)]


def _write_transcript(segments, path):
    # Transcription traduite lisible, avec horodatage [MM:SS] par segment.
    def _ts(t):
        return f'[{int(t // 60):02d}:{int(t % 60):02d}]'
    with open(path, 'w', encoding='utf-8') as f:
        f.write('VidNotes — Transcript\n' + '=' * 40 + '\n\n')
        for seg in segments:
            txt = seg['text'].strip()
            if txt:
                f.write(f'{_ts(seg["start"])} {txt}\n')


_LANG_NAMES = {
    'fr': 'French', 'en': 'English', 'ar': 'Arabic', 'es': 'Spanish', 'pt': 'Portuguese',
    'de': 'German', 'it': 'Italian', 'nl': 'Dutch', 'pl': 'Polish', 'el': 'Greek',
    'tr': 'Turkish', 'ru': 'Russian', 'zh': 'Chinese', 'ja': 'Japanese', 'ko': 'Korean',
    'hi': 'Hindi', 'bn': 'Bengali', 'ur': 'Urdu', 'fa': 'Persian', 'id': 'Indonesian',
    'vi': 'Vietnamese', 'th': 'Thai',
    'sw': 'Swahili', 'ha': 'Hausa', 'yo': 'Yoruba', 'ig': 'Igbo', 'am': 'Amharic',
    'om': 'Oromo', 'ti': 'Tigrinya', 'so': 'Somali', 'wo': 'Wolof', 'bm': 'Bambara',
    'fon': 'Fon', 'tw': 'Twi', 'ak': 'Akan', 'ee': 'Ewe', 'gaa': 'Ga', 'krio': 'Krio',
    'ln': 'Lingala', 'kg': 'Kikongo', 'rw': 'Kinyarwanda', 'run': 'Kirundi',
    'lg': 'Luganda', 'cgg': 'Rukiga', 'luo': 'Dholuo (Luo)', 'zu': 'Zulu', 'xh': 'Xhosa',
    'af': 'Afrikaans', 'nso': 'Sepedi', 'ts': 'Xitsonga', 've': 'Tshivenda',
    'st': 'Sesotho', 'tn': 'Setswana', 'ss': 'siSwati', 'sn': 'Shona', 'ny': 'Chichewa',
    'bem': 'Bemba', 'mg': 'Malagasy', 'mfe': 'Mauritian Creole', 'nus': 'Nuer',
}


def _study_summary(segments, tgt_lang):
    # Fiche de révision ultra-détaillée : l'IA lit TOUTE la transcription et
    # produit un résumé structuré qui couvre chaque partie essentielle du cours,
    # rédigé dans la langue cible. Traité par morceaux si le cours est long,
    # puis fusionné en une seule fiche cohérente.
    lang = _LANG_NAMES.get(tgt_lang, tgt_lang)
    lines = []
    for s in segments:
        t = s['text'].strip()
        if t:
            lines.append(f'[{int(s["start"] // 60):02d}:{int(s["start"] % 60):02d}] {t}')
    full = '\n'.join(lines)
    if not full:
        raise ValueError('transcription vide')

    sys_p = ('You are an expert academic note-taker. You turn lecture transcripts into '
             'exhaustive, well-structured study notes that let a student revise without '
             'rewatching the video. You never skip a section of the lecture.')

    def _notes(chunk, part_info=''):
        usr = (
            f'Write ULTRA-DETAILED study notes in {lang} for this lecture transcript{part_info}. '
            'Cover EVERY essential part in order — do not compress multiple topics into one line. '
            f'EVERYTHING must be written in {lang}, INCLUDING every section heading '
            f'(translate "Overview", "Detailed sections", "Key concepts & definitions", '
            f'"Key takeaways", "Self-check questions" and "Answer:" into natural {lang}). '
            'Return ONLY Markdown with exactly this structure:\n'
            '# (a clear title for the lecture)\n'
            '## Overview\n(3-5 sentences: what the lecture covers and why it matters)\n'
            '## Detailed sections\n'
            '(one ### heading per part of the lecture, WITH its [MM:SS] timestamp, followed by '
            'thorough bullet points: explanations, reasoning, examples given by the speaker, numbers, steps)\n'
            '## Key concepts & definitions\n(term — clear definition, one per line)\n'
            '## Key takeaways\n(the essential points to remember, as bullets)\n'
            '## Self-check questions\n(5 questions with answers hidden after "Answer:")\n\n'
            'TRANSCRIPT:\n' + chunk
        )
        return _strip_fences(_ds_chat(sys_p, usr, timeout=120))

    MAX = 24000
    if len(full) <= MAX:
        return _notes(full)
    # Cours long : notes par partie, puis fusion en une fiche unique.
    chunks = [full[i:i + MAX] for i in range(0, len(full), MAX)]
    partials = []
    for idx, ch in enumerate(chunks, 1):
        partials.append(_notes(ch, f' (part {idx} of {len(chunks)})'))
    merged = '\n\n'.join(partials)
    usr = (
        f'Merge these study notes (parts of the SAME lecture) into ONE coherent ultra-detailed '
        f'study sheet in {lang}, keeping the same Markdown structure (overview, detailed sections '
        f'with timestamps, key concepts, takeaways, self-check questions), all headings in {lang}. '
        'Keep ALL details and timestamps; remove only duplicates.\n\n' + merged[:60000]
    )
    return _strip_fences(_ds_chat(sys_p, usr, timeout=120))


def _split_for_subtitles(segments, max_chars=80):
    # Un sous-titre = 2 lignes de ~40 caracteres max. Tout segment plus long
    # est DECOUPE en plusieurs sous-titres successifs (timestamps repartis au
    # prorata du texte) au lieu d'etre tronque a l'affichage.
    out = []
    for seg in segments:
        text = ' '.join(seg['text'].split())
        if len(text) <= max_chars:
            out.append({'start': seg['start'], 'end': seg['end'], 'text': text})
            continue
        words = text.split()
        chunks, cur = [], []
        for w in words:
            if cur and len(' '.join(cur + [w])) > max_chars:
                chunks.append(' '.join(cur))
                cur = [w]
            else:
                cur.append(w)
        if cur:
            chunks.append(' '.join(cur))
        total = sum(len(c) for c in chunks) or 1
        t0, dur = seg['start'], max(0.3, seg['end'] - seg['start'])
        acc = 0
        for c in chunks:
            s_t = t0 + dur * (acc / total)
            acc += len(c)
            e_t = t0 + dur * (acc / total)
            out.append({'start': s_t, 'end': e_t, 'text': c})
    return out


def _write_srt(segments, path):
    segments = _split_for_subtitles(segments)
    def _fmt(t):
        h, m, s, ms = int(t // 3600), int((t % 3600) // 60), int(t % 60), int((t % 1) * 1000)
        return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'
    with open(path, 'w', encoding='utf-8') as f:
        for i, seg in enumerate(segments, 1):
            f.write(f'{i}\n{_fmt(seg["start"])} --> {_fmt(seg["end"])}\n{seg["text"]}\n\n')




# ─────────────────────────────────────────────────────────────────
# Sous-titres adaptatifs — toutes les styles via ASS natif
# ─────────────────────────────────────────────────────────────────

def _get_video_dims(video_path):
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
           '-show_entries', 'stream=width,height', '-of', 'csv=p=0', video_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        w, h = r.stdout.strip().split(',')
        return int(w), int(h)
    except Exception:
        return 1280, 720


def _wrap_line(text, max_chars=42):
    if len(text) <= max_chars:
        return text
    words, line, lines = text.split(), [], []
    for w in words:
        if sum(len(x) + 1 for x in line) + len(w) > max_chars and line:
            lines.append(' '.join(line))
            line = [w]
        else:
            line.append(w)
    if line:
        lines.append(' '.join(line))
    return r'\N'.join(lines[:2])


# Définition des 6 styles — toutes les valeurs en unités absolues ou multiplicateurs
# pc=PrimaryColour oc=OutlineColour bc=BackColour bs=BorderStyle
# sz_m=taille en % hauteur  ol_m=outline en % taille  sh_m=ombre en % taille
# mv_m=marginV en % hauteur  align=ASS alignment (2=bas-centre, 5=milieu-centre)
_ASS_STYLES = {
    # YouTube : blanc sur boite noire translucide
    # Lecture (VidNotes) : blanc gras sur boite sombre bien opaque — gros,
    # lisible sur n'importe quel fond, adapte aux cours.
    'broadcast': {
        'font': 'Noto Sans',    'bold': 1,  'sz_m': 0.056,
        'pc': '&H00FFFFFF', 'oc': '&H38000000', 'bc': '&H00000000',
        'bs': 3, 'ol_m': 0.28, 'sh_m': 0.0, 'align': 2, 'mv_m': 0.07,
    },
    # MrBeast : jaune gras, contour noir + ombre
    'mrbeast': {
        'font': 'Noto Sans',    'bold': 1,  'sz_m': 0.056,
        'pc': '&H0000E0FF', 'oc': '&H00000000', 'bc': '&H00000000',
        'bs': 1, 'ol_m': 0.16, 'sh_m': 0.07, 'align': 2, 'mv_m': 0.07,
    },
    # TikTok : blanc gras, contour noir + ombre
    'neon': {
        'font': 'Noto Sans',    'bold': 1,  'sz_m': 0.052,
        'pc': '&H00FFFFFF', 'oc': '&H00000000', 'bc': '&H00000000',
        'bs': 1, 'ol_m': 0.15, 'sh_m': 0.06, 'align': 2, 'mv_m': 0.07,
    },
    # Surlignage : texte noir sur boite jaune
    'highlight': {
        'font': 'Noto Sans',    'bold': 1,  'sz_m': 0.046,
        'pc': '&H00000000', 'oc': '&H0000FFFF', 'bc': '&H00000000',
        'bs': 3, 'ol_m': 0.2, 'sh_m': 0.0, 'align': 2, 'mv_m': 0.07,
    },
    # Cinema : blanc net, contour fin + ombre douce
    'center': {
        'font': 'Noto Sans',    'bold': 0,  'sz_m': 0.05,
        'pc': '&H00FFFFFF', 'oc': '&H00000000', 'bc': '&H00000000',
        'bs': 1, 'ol_m': 0.06, 'sh_m': 0.1, 'align': 2, 'mv_m': 0.07,
    },
    # Pop : rose gras, contour noir
    'matrix': {
        'font': 'Noto Sans',    'bold': 1,  'sz_m': 0.052,
        'pc': '&H00A02FFF', 'oc': '&H00000000', 'bc': '&H00000000',
        'bs': 1, 'ol_m': 0.14, 'sh_m': 0.05, 'align': 2, 'mv_m': 0.07,
    },
}



def _clean_sub_text(text):
    import re
    text = text.replace('--', '')
    text = re.sub(r'^[-–—\s]+', '', text)
    text = re.sub(r'[-–—\s]+$', '', text)
    text = re.sub(r'\\N[-–—\s]+', r'\\N', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()
def _hex_to_ass(hex_color):
    # '#RRGGBB' -> '&H00BBGGRR' (ordre ASS). None si invalide.
    if not hex_color:
        return None
    h = str(hex_color).strip().lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        int(h, 16)
    except ValueError:
        return None
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f'&H00{b}{g}{r}'.upper()


def _srt_to_ass(srt_path, ass_path, vid_w, vid_h, sub_style, size_scale=1.0, color_hex=None):
    d   = _ASS_STYLES.get(sub_style, _ASS_STYLES['mrbeast'])
    sz  = max(16, min(96, int(vid_h * d['sz_m'] * size_scale)))
    ol  = max(1, int(sz * d['ol_m'])) if d['ol_m'] > 0 else 0
    sh  = max(1, int(sz * d['sh_m'])) if d['sh_m'] > 0 else 0
    mv  = max(0, int(vid_h * d['mv_m']))
    pc  = _hex_to_ass(color_hex) or d['pc']

    # Safe zone : 5% de chaque côté (standard SMPTE/EBU = 10% total)
    # Broadcast : mv_m=0.0 → boîte colle au bas (standard TV)
    # Autres    : mv_m=0.05 → marge 5% du bas
    ml = mr = max(10, int(vid_w * 0.05))

    header = (
        '[Script Info]\nScriptType: v4.00+\n'
        f'PlayResX: {vid_w}\nPlayResY: {vid_h}\nWrapStyle: 1\n\n'
        '[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, '
        'OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, '
        'ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, '
        'Alignment, MarginL, MarginR, MarginV, Encoding\n'
        f'Style: Default,{d["font"]},{sz},{pc},&H000000FF,'
        f'{d["oc"]},{d["bc"]},'
        f'{d["bold"]},0,0,0,100,100,0,0,'
        f'{d["bs"]},{ol},{sh},{d["align"]},{ml},{mr},{mv},1\n\n'
        '[Events]\n'
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    )

    def srt2ass(t):
        t = t.strip().replace(',', '.')
        h2, m, rest = t.split(':')
        s, ms = rest.split('.')
        return f'{int(h2)}:{int(m):02d}:{int(s):02d}.{int(ms[:2]):02d}'

    events = []
    blocks = [b.strip() for b in open(srt_path, encoding='utf-8').read().strip().split('\n\n') if b.strip()]
    for blk in blocks:
        lns = blk.split('\n')
        arrow = next((i for i, l in enumerate(lns) if ' --> ' in l), None)
        if arrow is None:
            continue
        s_t, e_t = lns[arrow].split(' --> ')
        text = ' '.join(lns[arrow + 1:]).strip()
        text = _wrap_line(_clean_sub_text(text))
        an = r'{\an5}' if d['align'] == 5 else r'{\an2}'
        events.append(f'Dialogue: 0,{srt2ass(s_t)},{srt2ass(e_t)},Default,,0,0,0,,{an}{text}')

    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write(header + '\n'.join(events) + '\n')


# Mode embellissement : débruitage doux -> montée 1080p (lanczos) -> étalonnage
# couleur -> accentuation de netteté. 1080p MAX (borne le coût CPU/disque, pas de
# vrai 4K qui saturerait la VM). Pas de la super-résolution IA, juste un rendu
# plus net et plus joli, à un coût maîtrisé.
_ENH_CHAIN = ('hqdn3d=2:1:3:3,scale=-2:1080:flags=lanczos,'
              'eq=contrast=1.06:saturation=1.12:brightness=0.02:gamma=0.98,'
              'unsharp=5:5:0.8:3:3:0.4')


def _enhance_only(video_path, out_path):
    # Embellissement sans sous-titres (transcription vide). Repli copie si échec.
    cmd = ['ffmpeg', '-y', '-i', video_path, '-vf', _ENH_CHAIN,
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
           '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', out_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f'[enhance] échec, copie brute: {r.stderr[-300:]}', flush=True)
        shutil.copy2(video_path, out_path)


def _burn(video_path, srt_path, out_path, sub_style='mrbeast', size_scale=1.0, color_hex=None, enhance=False):
    if not os.path.exists(srt_path) or os.path.getsize(srt_path) == 0:
        if enhance:
            _enhance_only(video_path, out_path)
        else:
            shutil.copy2(video_path, out_path)
        return

    import uuid as _u
    vid_w, vid_h = _get_video_dims(video_path)

    # En mode embellissement la vidéo passe en 1080p : on génère l'ASS aux
    # dimensions FINALES pour que les sous-titres soient bien proportionnés.
    prefix = ''
    ass_w, ass_h = vid_w, vid_h
    if enhance and vid_h > 0:
        ass_h = 1080
        ass_w = max(2, int(round(vid_w * 1080 / vid_h)) // 2 * 2)
        prefix = _ENH_CHAIN + ','

    tmp_ass = f'/tmp/subs_{_u.uuid4().hex[:8]}.ass'
    _srt_to_ass(srt_path, tmp_ass, ass_w, ass_h, sub_style, size_scale, color_hex)

    # Escaper les apostrophes dans le chemin (au cas où)
    esc = tmp_ass.replace("'", "'\\''")
    vf  = prefix + (f"ass='{esc}'" if enhance else rf"scale=-2:min(720\,ih),ass='{esc}'")
    crf = '20' if enhance else '26'
    print(f'[burn] style={sub_style} enhance={enhance}', flush=True)

    cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-vf', vf,
        '-c:v', 'libx264', '-preset', ('veryfast' if enhance else 'ultrafast'), '-crf', crf, '-threads', '0',
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',   # moov atom at the front so browsers can stream it
        out_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        os.remove(tmp_ass)
    except Exception:
        pass
    if res.returncode != 0:
        raise RuntimeError(f'FFmpeg failed:\n{res.stderr[-600:]}')