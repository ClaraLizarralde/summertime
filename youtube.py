#!/usr/bin/env python3
"""
Paso 2: vincular las recordings de versiones.db con videos de YouTube.

Uso:
  python3 02_youtube.py buscar --max 3      # prueba chica (3 búsquedas)
  python3 02_youtube.py buscar              # tanda normal (95 búsquedas)
  python3 02_youtube.py revisar             # lista los resultados dudosos
  python3 02_youtube.py resumen             # cuántas filas hay por estado

La clave de API se lee de la variable YOUTUBE_API_KEY o del archivo
youtube_key.txt (en la misma carpeta que este script). NO la compartas.

Estados de youtube_status:
  pending         todavía no se buscó
  ok              video encontrado, embebible y que parece el correcto
  review          hay un video pero es dudoso: revisar a ojo
  not_embeddable  hay videos pero ninguno se puede embeber
  not_found       no apareció nada
  error           falló la búsqueda (se reintenta en la próxima tanda)

Costo de cuota: search.list = 100 unidades, videos.list = 1 unidad.
Cada artista+título cuesta 101 -> unas 99 por día con las 10.000 gratis.
Las filas repetidas (mismo artista y título) comparten un solo resultado.
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

API = "https://www.googleapis.com/youtube/v3"
CARPETA = os.path.dirname(os.path.abspath(__file__))

# Palabras que no sirven para reconocer a un artista
STOP = {"the", "and", "his", "her", "orchestra", "band", "trio", "quartet",
        "quintet", "with", "feat", "featuring", "ft", "los", "las", "el",
        "la", "of", "et", "jr", "sr"}
# Si el título del video las tiene, queda como "review"
MALAS = ["karaoke", "tutorial", "lesson", "backing track", "how to play",
         "cover", "reaction", "remix"]

SEG_MIN, SEG_MAX = 60, 720  # videos de 1 a 12 minutos (evita discos enteros)


class CuotaAgotada(Exception):
    pass


# ------------------------------------------------------------ utilidades
def leer_clave():
    clave = os.environ.get("YOUTUBE_API_KEY")
    if not clave:
        ruta = os.path.join(CARPETA, "youtube_key.txt")
        if os.path.exists(ruta):
            with open(ruta, encoding="utf-8") as f:
                clave = f.read().strip()
    if not clave:
        sys.exit("No encuentro la clave de YouTube.\n"
                 "Creá un archivo youtube_key.txt en esta carpeta con la "
                 "clave adentro (solo la clave, nada más).")
    return clave


def api_get(endpoint, params, clave):
    params = {**params, "key": clave}
    url = f"{API}/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")
        if e.code == 403 and "quotaExceeded" in cuerpo:
            raise CuotaAgotada()
        # no se imprime la URL para no mostrar la clave
        raise RuntimeError(f"HTTP {e.code}: {cuerpo[:300]}")


def norm(texto):
    t = unicodedata.normalize("NFKD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", t.lower())


def tokens_artista(artista):
    principal = re.split(r",|;|\bfeat\b\.?|\bft\b\.?", artista, flags=re.I)[0]
    return [w for w in norm(principal).split()
            if len(w) >= 3 and w not in STOP]


def segundos(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def evaluar(cand, artista, palabra):
    """Devuelve (puntaje, es_ok) para un video candidato."""
    titulo = norm(cand["titulo"])
    canal = norm(cand["canal"])
    palabras_video = set((titulo + " " + canal).split())
    toks = tokens_artista(artista)
    ratio = (sum(t in palabras_video for t in toks) / len(toks)) if toks else 0
    tiene_palabra = norm(palabra).strip() in titulo
    mala = any(re.search(rf"\b{m}\b", titulo) for m in MALAS)
    dur_ok = SEG_MIN <= cand["segundos"] <= SEG_MAX
    topic = canal.strip().endswith("topic")  # canal oficial autogenerado
    puntaje = (2 * ratio + (1 if tiene_palabra else 0)
               + (0.5 if topic else 0)
               - (1 if mala else 0) - (1 if not dur_ok else 0))
    es_ok = ratio >= 0.5 and tiene_palabra and not mala and dur_ok
    return puntaje, es_ok


# ------------------------------------------------------------- búsqueda
def procesar(artista, titulo, clave, palabra):
    """Busca un video. Devuelve (youtube_id o None, estado)."""
    q = f"{artista[:60]} {titulo}"
    s = api_get("search", {"part": "snippet", "type": "video",
                           "videoEmbeddable": "true", "maxResults": 5,
                           "q": q}, clave)
    ids = [i["id"]["videoId"] for i in s.get("items", [])]
    if not ids:
        return None, "not_found"

    v = api_get("videos", {"part": "snippet,contentDetails,status",
                           "id": ",".join(ids)}, clave)
    cands, hubo_no_embebible = [], False
    for it in v.get("items", []):
        st = it.get("status", {})
        if not st.get("embeddable", False) or st.get("privacyStatus") != "public":
            hubo_no_embebible = True
            continue
        cands.append({
            "id": it["id"],
            "titulo": it["snippet"]["title"],
            "canal": it["snippet"]["channelTitle"],
            "segundos": segundos(it["contentDetails"].get("duration")),
        })
    if not cands:
        return None, ("not_embeddable" if hubo_no_embebible else "not_found")

    mejor = max(cands, key=lambda c: evaluar(c, artista, palabra)[0])
    _, es_ok = evaluar(mejor, artista, palabra)
    return mejor["id"], ("ok" if es_ok else "review")


def cmd_buscar(db, maximo, palabra):
    clave = leer_clave()
    con = sqlite3.connect(db)
    grupos = con.execute("""
        SELECT artist, title, COUNT(*) n FROM recordings
        WHERE youtube_status IN ('pending', 'error') AND artist IS NOT NULL
        GROUP BY artist, title
        ORDER BY n DESC, artist
    """).fetchall()
    print(f"Pendientes: {len(grupos)} (artista+título). "
          f"Esta tanda: hasta {maximo}.\n")

    hechos, errores_seguidos = 0, 0
    for artista, titulo, n in grupos[:maximo]:
        try:
            yid, estado = procesar(artista, titulo, clave, palabra)
            errores_seguidos = 0
        except CuotaAgotada:
            print("\nSe agotó la cuota diaria de YouTube. "
                  "Seguí mañana: el script retoma donde quedó.")
            break
        except Exception as e:
            yid, estado = None, "error"
            errores_seguidos += 1
            print(f"  ERROR: {e}")
            if errores_seguidos >= 3:
                print("\n3 errores seguidos: freno. Revisá el mensaje de "
                      "arriba (¿clave mal copiada? ¿API habilitada?).")
                break

        con.execute("""
            UPDATE recordings
            SET youtube_id = ?, youtube_status = ?,
                youtube_checked_at = datetime('now')
            WHERE artist = ? AND title = ?
              AND youtube_status IN ('pending', 'error')
        """, (yid, estado, artista, titulo))
        con.commit()
        hechos += 1
        print(f"[{hechos}] {artista} - {titulo} ({n} filas) -> {estado}"
              + (f"  https://youtu.be/{yid}" if yid else ""))
        time.sleep(0.2)

    print(f"\nTanda terminada: {hechos} búsquedas.")
    cmd_resumen(db)


# -------------------------------------------------------------- reportes
def cmd_resumen(db):
    con = sqlite3.connect(db)
    print("\nFilas por estado:")
    for estado, n in con.execute("""
            SELECT youtube_status, COUNT(*) FROM recordings
            GROUP BY youtube_status ORDER BY COUNT(*) DESC"""):
        print(f"  {estado:15} {n}")


def cmd_revisar(db):
    con = sqlite3.connect(db)
    filas = con.execute("""
        SELECT DISTINCT artist, title, youtube_id FROM recordings
        WHERE youtube_status = 'review' ORDER BY artist""").fetchall()
    print(f"{len(filas)} resultados dudosos para revisar a ojo:\n")
    for artista, titulo, yid in filas:
        print(f"{artista} - {titulo}\n   https://youtu.be/{yid}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("comando", choices=["buscar", "revisar", "resumen"])
    ap.add_argument("--db", default="versiones.db")
    ap.add_argument("--max", type=int, default=95,
                    help="máximo de búsquedas en esta tanda (default 95)")
    ap.add_argument("--palabra", default="summertime",
                    help="palabra que debe aparecer en el título del video")
    a = ap.parse_args()

    if a.comando == "buscar":
        cmd_buscar(a.db, a.max, a.palabra)
    elif a.comando == "revisar":
        cmd_revisar(a.db)
    else:
        cmd_resumen(a.db)