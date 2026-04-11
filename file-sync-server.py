#!/usr/bin/env python3
"""ChartHound File Sync Server v7.0 - Uses metaflac for FLAC writes"""
import os, subprocess, logging
from flask import Flask, request, jsonify
try:
    from mutagen.id3 import ID3, TCON, TDRC, TXXX, ID3NoHeaderError
    from mutagen.mp4 import MP4
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
except ImportError:
    print("ERROR: pip install mutagen flask --break-system-packages")
    exit(1)

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)
app = Flask(__name__)

@app.after_request
def add_cors(r):
    r.headers['Access-Control-Allow-Origin'] = '*'
    r.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return r

@app.route('/ping', methods=['GET','OPTIONS'])
def ping(): return jsonify({'status':'ok','version':'7.1-metaflac'})

@app.route('/write', methods=['POST','OPTIONS'])
def write_tags():
    if request.method == 'OPTIONS': return jsonify({'ok':True})
    data = request.get_json()
    matches = data.get('matches', [])
    written = failed = 0
    errors = []
    log.info(f"Write request: {len(matches)} files")
    for m in matches:
        fp = m.get('filepath','')
        if not fp or not os.path.exists(fp):
            failed += 1; errors.append(f"Not found: {fp}"); continue
        try:
            ext = os.path.splitext(fp)[1].lower()
            g = m.get('genres',[]); mo = m.get('moods',[]); y = m.get('year')
            if ext == '.flac': write_flac(fp, g, mo, y)
            elif ext == '.mp3': write_mp3(fp, g, mo, y)
            elif ext in ('.m4a','.aac','.mp4'): write_m4a(fp, g, mo, y)
            else: write_generic(fp, g, mo, y)
            written += 1
        except Exception as e:
            failed += 1; msg = f"{os.path.basename(fp)}: {e}"
            errors.append(msg); log.error(f"ERROR {msg}")
    log.info(f"Write complete: {written} ok, {failed} failed")
    return jsonify({'written':written,'failed':failed,'errors':errors[:20]})

def write_flac(fp, genres, moods, year, replace=True):
    log.debug(f"  Writing FLAC: {os.path.basename(fp)} genres={genres}")
    cmd = ["metaflac"]
    if genres: cmd += ["--remove-tag=GENRE"]
    if moods:  cmd += ["--remove-tag=MOOD"]
    if year:   cmd += ["--remove-tag=DATE"]
    for g in (genres or []): cmd += [f"--set-tag=GENRE={g}"]
    for mo in (moods or []): cmd += [f"--set-tag=MOOD={mo}"]
    if year: cmd += [f"--set-tag=DATE={year}"]
    cmd += [fp]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"metaflac failed: {result.stderr.strip()}")
    # Verify using metaflac (reads actual bytes, not cached)
    verify_result = subprocess.run(
        ["metaflac", "--list", "--block-type=VORBIS_COMMENT", fp],
        capture_output=True, text=True
    )
    got_genres = [line.split("GENRE=")[1].strip() 
                  for line in verify_result.stdout.splitlines() 
                  if "GENRE=" in line]
    if genres and got_genres != genres:
        log.warning(f"  VERIFY FAIL: {os.path.basename(fp)} expected={genres} got={got_genres}")
    else:
        log.debug(f"  VERIFY OK: {os.path.basename(fp)} genre={got_genres}")

def write_mp3(fp, genres, moods, year, replace=True):
    try: tags = ID3(fp)
    except ID3NoHeaderError: tags = ID3()
    if genres: tags['TCON'] = TCON(encoding=3, text=['; '.join(genres)])
    if moods: tags['TXXX:MOOD'] = TXXX(encoding=3, desc='MOOD', text=['; '.join(moods)])
    if year: tags['TDRC'] = TDRC(encoding=3, text=[str(year)])
    tags.save(fp, v2_version=3)

def write_m4a(fp, genres, moods, year, replace=True):
    f = MP4(fp)
    if genres: f['\xa9gen'] = ['; '.join(genres)]
    if year: f['\xa9day'] = [str(year)]
    f.save()

def write_generic(fp, genres, moods, year, replace=True):
    f = MutagenFile(fp, easy=True)
    if f is None: raise ValueError("Unsupported format")
    if genres: f['genre'] = genres
    if year:
        try: f['date'] = [str(year)]
        except: pass
    f.save()

if __name__ == '__main__':
    print("="*55)
    print("  ChartHound File Sync Server v7.1")
    print("  Uses metaflac for reliable FLAC writes")
    print("="*55)
    app.run(host='0.0.0.0', port=4321, debug=False, threaded=False)
