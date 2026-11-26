#!/usr/bin/env python3
"""
MusicBrainz -> SQLite

Uso:
  python 01_musicbrainz_to_sqlite.py buscar "Summertime" Gershwin
      Lista works candidatas (con su MBID) para elegir la correcta.

  python 01_musicbrainz_to_sqlite.py bajar <WORK_MBID> [--db versiones.db]
      Descarga todas las recordings de esa work y las guarda en SQLite.

Es re-ejecutable: hace upsert por mbid y NO pisa las columnas de YouTube.
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

# >>> PONÉ TU EMAIL ACÁ (MusicBrainz lo exige) <<<
USER_AGENT = "VersionesExplorer/0.1 ( claralizarra@gmail.com )"

BASE = "https://musicbrainz.org/ws/2"
PAGE = 25  # pedidos chicos = menos timeouts

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    mbid               TEXT NOT NULL UNIQUE,
    work_mbid          TEXT NOT NULL,
    title              TEXT NOT NULL,
    artist             TEXT,
    year               INTEGER,
    first_release_date TEXT,
    album              TEXT,
    disambiguation     TEXT,
    length_ms          INTEGER,
    youtube_id         TEXT,
    youtube_status     TEXT NOT NULL DEFAULT 'pending',
    youtube_checked_at TEXT,
    genre              TEXT,
    fetched_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_last_call = 0.0


def mb_get(path, params):
    """GET a la API respetando 1 pedido/segundo y reintentando en 503."""
    global _last_call
    params = {**params, "fmt": "json"}
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    for intento in range(5):
        espera = 1.1 - (time.time() - _last_call)
        if espera > 0:
            time.sleep(espera)
        _last_call = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 429):
                print(f"  (HTTP {e.code}, reintento {intento + 1}/5...)")
                time.sleep(3 * (intento + 1))
                continue
            cuerpo = e.read().decode("utf-8", "replace")[:300]
            sys.exit(f"Error HTTP {e.code} de MusicBrainz: {cuerpo}\nURL: {url}")
        except (TimeoutError, urllib.error.URLError) as e:
            print(f"  (sin respuesta, reintento {intento + 1}/5...)")
            time.sleep(3 * (intento + 1))
            continue
    raise RuntimeError("MusicBrainz no responde tras 5 intentos")


def artist_credit_text(rec):
    partes = []
    for ac in rec.get("artist-credit", []):
        partes.append(ac.get("name") or ac["artist"]["name"])
        partes.append(ac.get("joinphrase", ""))
    return "".join(partes).strip() or None


def earliest_release(rec):
    """Devuelve (fecha_mas_antigua, titulo_del_album_de_esa_fecha)."""
    fecha = rec.get("first-release-date") or None
    album = None
    candidatos = [
        (r.get("date"), r.get("title"))
        for r in rec.get("releases", [])
        if r.get("date")
    ]
    if candidatos:
        candidatos.sort()
        f, a = candidatos[0]
        fecha = fecha or f
        album = a
    elif rec.get("releases"):
        album = rec["releases"][0].get("title")
    return fecha, album


def genres_text(rec):
    g = rec.get("genres") or []
    g = sorted(g, key=lambda x: -x.get("count", 0))
    return ", ".join(x["name"] for x in g) or None


# ---------------------------------------------------------------- buscar
def cmd_buscar(titulo, artista):
    q = f'work:"{titulo}"'
    if artista:
        q += f' AND artist:"{artista}"'
    data = mb_get("work", {"query": q, "limit": 15})
    for w in data.get("works", []):
        arts = [
            rel.get("artist", {}).get("name")
            for rel in w.get("relations", [])
            if rel.get("artist")
        ]
        print(f"{w['id']}  score={w.get('score')}  {w.get('title')!r}"
              f"  tipo={w.get('type')}  {w.get('disambiguation', '')}")
    print("\nElegí el MBID correcto y corré: bajar <MBID>")


# ----------------------------------------------------------------- bajar
def cmd_bajar(work_mbid, db_path):
    # 1) Confirmar qué obra es
    work = mb_get(f"work/{work_mbid}", {"inc": "artist-rels"})
    print(f"Work: {work.get('title')!r}  ({work_mbid})")
    for rel in work.get("relations", []):
        if rel.get("artist"):
            print(f"  {rel['type']}: {rel['artist']['name']}")
    if input("¿Es la obra correcta? [s/N] ").strip().lower() != "s":
        sys.exit("Cancelado.")

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    # 2) Browse de recordings por work, paginado
    offset, total, guardadas = 0, None, 0
    while total is None or offset < total:
        data = mb_get("recording", {
            "work": work_mbid,
            "inc": "artist-credits genres",
            "limit": PAGE,
            "offset": offset,
        })
        total = data["recording-count"]
        recs = data["recordings"]
        if not recs:
            break
        for rec in recs:
            fecha, album = earliest_release(rec)
            year = int(fecha[:4]) if fecha and fecha[:4].isdigit() else None
            con.execute(
                """
                INSERT INTO recordings
                    (mbid, work_mbid, title, artist, year, first_release_date,
                     album, disambiguation, length_ms, genre)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(mbid) DO UPDATE SET
                    work_mbid=excluded.work_mbid,
                    title=excluded.title,
                    artist=excluded.artist,
                    year=excluded.year,
                    first_release_date=excluded.first_release_date,
                    album=excluded.album,
                    disambiguation=excluded.disambiguation,
                    length_ms=excluded.length_ms,
                    genre=COALESCE(recordings.genre, excluded.genre),
                    fetched_at=datetime('now')
                """,
                (rec["id"], work_mbid, rec["title"], artist_credit_text(rec),
                 year, fecha, album, rec.get("disambiguation") or None,
                 rec.get("length"), genres_text(rec)),
            )
            guardadas += 1
        con.commit()
        offset += PAGE
        print(f"  {min(offset, total)}/{total}")

    n = con.execute("SELECT COUNT(*) FROM recordings WHERE work_mbid=?",
                    (work_mbid,)).fetchone()[0]
    print(f"\nListo. Procesadas {guardadas}; en la base hay {n} para esta work.")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("buscar")
    b.add_argument("titulo")
    b.add_argument("artista", nargs="?")

    d = sub.add_parser("bajar")
    d.add_argument("work_mbid")
    d.add_argument("--db", default="versiones.db")

    a = ap.parse_args()
    if "tu_email" in USER_AGENT:
        sys.exit("Editá USER_AGENT con tu email antes de correr el script.")
    if a.cmd == "buscar":
        cmd_buscar(a.titulo, a.artista)
    else:
        cmd_bajar(a.work_mbid, a.db)