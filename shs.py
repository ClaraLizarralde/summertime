#!/usr/bin/env python3
"""
SecondHandSongs -> SQLite (tabla shs_versions dentro de versiones.db)

Uso:
  py shs.py traer                  # baja las versiones de la obra 4681 (Summertime)
  py shs.py traer --work 12345     # otra obra
  py shs.py comparar               # cuántas ya están en tu base de MusicBrainz

No toca la tabla recordings: crea una tabla aparte (shs_versions).
Sin clave de API el límite es de 20 pedidos por minuto, 200 por hora y 1000
por día. Si conseguís una clave, ponela en shs_key.txt (solo la clave) o en la
variable SHS_API_KEY. NO la subas a GitHub.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.secondhandsongs.com"
USER_AGENT = "VersionesExplorer/0.1 ( claralizarra@gmail.com )"
CARPETA = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS shs_versions (
    performance_id INTEGER PRIMARY KEY,
    work_id        INTEGER NOT NULL,
    title          TEXT,
    artist         TEXT,
    is_original    INTEGER,
    shs_url        TEXT,
    in_musicbrainz INTEGER,
    fetched_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def leer_clave():
    clave = os.environ.get("SHS_API_KEY")
    if not clave:
        ruta = os.path.join(CARPETA, "shs_key.txt")
        if os.path.exists(ruta):
            with open(ruta, encoding="utf-8") as f:
                clave = f.read().strip()
    return clave or None


def get_json(path, params=None):
    params = {**(params or {}), "format": "json"}
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    clave = leer_clave()
    if clave:
        headers["X-API-Key"] = clave
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")[:300]
        sys.exit(f"Error HTTP {e.code} de SecondHandSongs: {cuerpo}")


def norm(texto):
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", t.lower()).split())


def resumen_json(data):
    """Muestra la forma del JSON para detectar paginación."""
    print("\nClaves del JSON:")
    for k, v in data.items():
        if isinstance(v, list):
            print(f"  {k}: lista de {len(v)}")
        elif isinstance(v, dict):
            print(f"  {k}: objeto con claves {list(v.keys())[:8]}")
        else:
            print(f"  {k}: {str(v)[:80]}")


def cmd_traer(db, work):
    data = get_json(f"/work/{work}")
    ruta = os.path.join(CARPETA, f"shs_work_{work}.json")
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"Obra: {data.get('title')!r} (id {work}). "
          f"Respuesta completa guardada en {os.path.basename(ruta)}")
    resumen_json(data)

    versiones = data.get("versions", [])
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    guardadas = 0
    for v in versiones:
        m = re.search(r"/performance/(\d+)", v.get("uri", ""))
        if not m:
            continue
        pid = int(m.group(1))
        p = v.get("performer") or {}
        con.execute("""
            INSERT INTO shs_versions
                (performance_id, work_id, title, artist, is_original, shs_url)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(performance_id) DO UPDATE SET
                title=excluded.title, artist=excluded.artist,
                is_original=excluded.is_original, shs_url=excluded.shs_url
        """, (pid, work, v.get("title"), p.get("name"),
              1 if p.get("isOriginal") else 0,
              f"https://secondhandsongs.com/performance/{pid}"))
        guardadas += 1
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM shs_versions WHERE work_id=?",
                        (work,)).fetchone()[0]
    print(f"\nVersiones recibidas: {len(versiones)}. "
          f"En la base hay {total} para esta obra.")
    print("Si la web dice bastantes más, la lista viene paginada: "
          "pegame el bloque 'Claves del JSON' de arriba.")


def cmd_comparar(db):
    con = sqlite3.connect(db)
    mb = {norm(r[0]) for r in con.execute(
        "SELECT DISTINCT artist FROM recordings WHERE artist IS NOT NULL")}
    filas = con.execute(
        "SELECT performance_id, artist FROM shs_versions").fetchall()
    if not filas:
        sys.exit("Primero corré: py shs.py traer")
    nuevas = []
    for pid, artista in filas:
        esta = norm(artista) in mb
        con.execute("UPDATE shs_versions SET in_musicbrainz=? "
                    "WHERE performance_id=?", (1 if esta else 0, pid))
        if not esta:
            nuevas.append(artista)
    con.commit()
    print(f"Versiones de SecondHandSongs: {len(filas)}")
    print(f"  con artista que ya está en tu base: {len(filas) - len(nuevas)}")
    print(f"  con artista NUEVO: {len(nuevas)}")
    print("\nAlgunos artistas nuevos:")
    for a in nuevas[:25]:
        print("  ", a)
    print("\n(Comparación aproximada: por nombre. Un mismo artista escrito "
          "distinto puede figurar como nuevo.)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("comando", choices=["traer", "comparar"])
    ap.add_argument("--db", default="versiones.db")
    ap.add_argument("--work", type=int, default=4681)
    a = ap.parse_args()
    if a.comando == "traer":
        cmd_traer(a.db, a.work)
    else:
        cmd_comparar(a.db)