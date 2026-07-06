#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pipeline maquette LiDAR HD -> GLB (Verdict).

Usage :
    python pipeline/pipeline.py --lat 45.7706 --lon 4.8330 [--label "8 rue du Griffon"] [--dry-run]

Etapes :
    1. lat/lon -> Lambert-93, centre snappe a la grille 100 m, cle de site,
       fenetre carree +/-250 m autour du centre snappe.
    2. WFS IGN : liste des dalles LiDAR HD (COPC) intersectant la fenetre.
    3. --dry-run : imprime le plan JSON et sort (code 0).
    4. Telechargement COMPLET de chaque dalle dans dalles_cache/ (cache par taille).
    5. Crop local CopcReader sur la fenetre, fusion multi-dalles -> work/crop.laz.
    6. Emprises BDTOPO_V3:batiment -> work/footprints.geojson (0 emprise -> code 3).
    7. roofer --lod22 -> work/roofer_out/*.city.jsonl.
    8. Maillage batiments (earcut avec trous) + maillage sol (DTM classe 2,
       grille 1 m, trous combles par plus-proche-voisin, median 3 + uniform 3).
    9. Scene GLB {batiments, sol}, recentrage X-CX/Y-CY (centre snappe),
       Z = altitude absolue, materiaux gris via post-traitement du GLB.
   10. Mise a jour index.json, derniere ligne stdout : MAQUETTE_RESULT <json>.

Environnement : ROOFER_BIN (defaut <racine du depot>/roofer/bin/roofer).
Codes de sortie : 0 = OK, 3 = aucun batiment dans la fenetre, 1 = erreur.
"""
import argparse
import gzip
import datetime
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import time

import numpy as np
import requests
from pyproj import Transformer

# ---------------------------------------------------------------- constantes
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # racine du depot
WFS = "https://data.geopf.fr/wfs/ows"
SIDE_DEFAUT = 500.0     # cote par defaut de la fenetre (m) ; --side pour changer (cle suffixee _w<cote>)
SNAP = 100              # pas de la grille de snap du centre (m)
GROUND_GRID = 1.0       # resolution du DTM sol (m)
CHUNK = 8 * 1024 * 1024  # 8 Mo
COLOR_BATIMENTS = [0.82, 0.80, 0.78, 1.0]
COLOR_SOL = [0.55, 0.56, 0.54, 1.0]
RAW_URL = ("https://raw.githubusercontent.com/romain800richardeau-dot/"
           "verdict-maquettes-lidar/main/glb/%s.glb")

DALLES_CACHE = os.path.join(BASE, "dalles_cache")
WORK = os.path.join(BASE, "work")
GLB_DIR = os.path.join(BASE, "glb")
INDEX = os.path.join(BASE, "index.json")


# -------------------------------------------------------------------- utils
def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fail(msg, code=1):
    log("ERREUR : %s" % msg)
    sys.exit(code)


class Chrono:
    """Chronometre + log d'etape sur stderr."""

    def __init__(self, label):
        self.label = label

    def __enter__(self):
        self.t0 = time.perf_counter()
        log("[%s] debut" % self.label)
        return self

    def __exit__(self, exc_type, exc, tb):
        dt = time.perf_counter() - self.t0
        if exc_type is None:
            log("[%s] fin en %.1f s" % (self.label, dt))
        else:
            log("[%s] ECHEC apres %.1f s : %s" % (self.label, dt, exc))
        return False


# ------------------------------------------------------- etape 1 : geometrie
def site_geometry(lat, lon, side):
    tr = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    cx, cy = tr.transform(lon, lat)
    snx = int(round(cx / SNAP)) * SNAP
    sny = int(round(cy / SNAP)) * SNAP
    key = "e%d_n%d" % (snx, sny)
    if side != SIDE_DEFAUT:
        key += "_w%d" % int(side)      # les cles 500 m historiques restent SANS suffixe (retro-compatibilite)
    half = side / 2.0
    window = (snx - half, sny - half, snx + half, sny + half)
    return cx, cy, snx, sny, key, window


# ---------------------------------------------------- etape 2 : dalles (WFS)
def wfs_dalles(window):
    params = {
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "TYPENAMES": "IGNF_NUAGES-DE-POINTS-LIDAR-HD:dalle",
        "OUTPUTFORMAT": "application/json", "COUNT": "500",
        "BBOX": "%f,%f,%f,%f,urn:ogc:def:crs:EPSG::2154" % window,
    }
    r = requests.get(WFS, params=params, timeout=60)
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        if p.get("url"):
            out.append({"name": p.get("name_download") or p["url"].split("/")[-1],
                        "url": p["url"]})
    # deterministe (l'ordre WFS ne l'est pas forcement)
    out.sort(key=lambda d: d["name"])
    return out


# ------------------------------------------------ etape 4 : telechargements
def download_dalle(url, dest):
    """Telechargement complet en stream, cache par taille, 2 reprises."""
    last_err = None
    for attempt in range(1, 4):
        try:
            with requests.get(url, stream=True, timeout=(30, 600)) as r:
                r.raise_for_status()
                size = int(r.headers.get("Content-Length", "0") or 0)
                if size and os.path.exists(dest) and os.path.getsize(dest) == size:
                    log("  cache OK (%s, %.1f Mo), pas de retelechargement"
                        % (os.path.basename(dest), size / 1048576.0))
                    return size, 0.0, True
                t0 = time.perf_counter()
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        if chunk:
                            f.write(chunk)
                got = os.path.getsize(tmp)
                if size and got != size:
                    raise IOError("taille incomplete : %d octets sur %d" % (got, size))
                os.replace(tmp, dest)
                dt = time.perf_counter() - t0
                log("  %s : %.1f Mo en %.1f s" % (os.path.basename(dest), got / 1048576.0, dt))
                return got, dt, False
        except Exception as e:
            last_err = e
            log("  tentative %d/3 echouee (%s)" % (attempt, e))
            time.sleep(2 * attempt)
    fail("telechargement impossible pour %s : %s" % (url, last_err))


# -------------------------------------------------------- etape 5 : crop LAZ
def crop_and_merge(local_paths, window, out_laz):
    import laspy
    from laspy.copc import Bounds

    minx, miny, maxx, maxy = window
    bounds = Bounds(mins=np.array([minx, miny, -1e4]),
                    maxs=np.array([maxx, maxy, 1e4]))
    # ECRITURE EN FLUX : une dalle a la fois en memoire (obligatoire pour les grandes
    # fenetres : 3,2 km = jusqu'a 25 dalles, la fusion en RAM ferait deborder le runner).
    # PIEGE laspy inchange : pas d'assignation .x sur PackedPointRecord, passer par les
    # entiers X/Y/Z rescales vers les scales/offsets de la 1re dalle.
    total = 0
    n_ground = 0
    n_bat = 0
    writer = None
    h = None
    skip = {"X", "Y", "Z"}
    warned = set()
    try:
        for path in local_paths:
            ta = time.perf_counter()
            with laspy.CopcReader.open(path) as cr:
                pts = cr.query(bounds)
                if h is None:
                    h = laspy.LasHeader(version=cr.header.version,
                                        point_format=cr.header.point_format)
                    h.offsets = cr.header.offsets
                    h.scales = cr.header.scales
                    writer = laspy.open(out_laz, mode="w", header=h)
            npts = len(pts)
            log("  %s : %d points dans la fenetre (%.1f s)"
                % (os.path.basename(path), npts, time.perf_counter() - ta))
            if not npts:
                continue
            rec = laspy.PackedPointRecord.zeros(npts, h.point_format)
            rec["X"] = np.round((np.asarray(pts.x) - h.offsets[0]) / h.scales[0]).astype(np.int32)
            rec["Y"] = np.round((np.asarray(pts.y) - h.offsets[1]) / h.scales[1]).astype(np.int32)
            rec["Z"] = np.round((np.asarray(pts.z) - h.offsets[2]) / h.scales[2]).astype(np.int32)
            for dim in h.point_format.dimension_names:
                if dim in skip:
                    continue
                try:
                    rec[dim] = np.asarray(pts[dim])
                except Exception as e:
                    if dim not in warned:
                        warned.add(dim)
                        log("  dim %s non copiee : %s" % (dim, e))
            writer.write_points(rec)
            cls = np.asarray(pts.classification)
            total += npts
            n_ground += int((cls == 2).sum())
            n_bat += int((cls == 6).sum())
            del pts, rec
    finally:
        if writer is not None:
            writer.close()
    if not total:
        fail("aucun point LiDAR dans la fenetre (dalles vides sur cette emprise)")
    log("  fusion en flux : %d points (%d sol classe 2, %d bati classe 6) -> %s"
        % (total, n_ground, n_bat, out_laz))
    return total, n_ground


# ------------------------------------------------- etape 6 : emprises BDTOPO
def fetch_footprints(window, out_geojson):
    params = {
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "TYPENAMES": "BDTOPO_V3:batiment", "OUTPUTFORMAT": "application/json",
        "SRSNAME": "EPSG:2154", "COUNT": "10000",
        "BBOX": "%f,%f,%f,%f,urn:ogc:def:crs:EPSG::2154" % window,
    }
    r = requests.get(WFS, params=params, timeout=120)
    r.raise_for_status()
    fc = r.json()
    # membre crs requis pour qu'OGR (roofer) lise le Lambert-93
    fc["crs"] = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::2154"}}
    n = len(fc.get("features", []))
    with open(out_geojson, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    log("  %d emprises -> %s" % (n, out_geojson))
    return n


# ----------------------------------------------------------- etape 7 : roofer
def run_roofer(roofer_bin, crop_laz, footprints, outdir, logfile):
    if not os.path.isfile(roofer_bin) and os.path.isfile(roofer_bin + ".exe"):
        roofer_bin = roofer_bin + ".exe"
    if not os.path.isfile(roofer_bin):
        fail("binaire roofer introuvable : %s (definir ROOFER_BIN)" % roofer_bin)
    bin_dir = os.path.dirname(os.path.abspath(roofer_bin))
    share = os.path.join(os.path.dirname(bin_dir), "share")
    env = dict(os.environ)
    env["GDAL_DATA"] = os.path.join(share, "gdal")
    env["PROJ_LIB"] = os.path.join(share, "proj")
    for k in ("GDAL_DATA", "PROJ_LIB"):
        log("  %s=%s%s" % (k, env[k], "" if os.path.isdir(env[k]) else " (ABSENT)"))
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir, exist_ok=True)
    cmd = [roofer_bin, "--lod22", crop_laz, footprints, outdir]
    log("  " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, env=env, timeout=1800,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.TimeoutExpired:
        fail("roofer : depassement du delai de 1800 s")
    with open(logfile, "wb") as f:
        f.write(proc.stdout or b"")
    log("  log roofer -> %s (%d octets)" % (logfile, len(proc.stdout or b"")))
    if proc.returncode != 0:
        tail = (proc.stdout or b"")[-2000:].decode("utf-8", "replace")
        fail("roofer code retour %d ; fin du log :\n%s" % (proc.returncode, tail))
    n_feat = 0
    files = cityjson_files(outdir)
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    if json.loads(ln).get("type") == "CityJSONFeature":
                        n_feat += 1
                except Exception:
                    pass
    log("  %d fichiers CityJSON, %d features" % (len(files), n_feat))
    if not files:
        fail("roofer n'a produit aucun fichier CityJSON dans %s" % outdir)
    return n_feat


def cityjson_files(outdir):
    return (glob.glob(os.path.join(outdir, "*.jsonl"))
            + glob.glob(os.path.join(outdir, "*.json")))


# ------------------------------------------- etape 8a : maillage des batiments
def tri_surface(rings_pts):
    """Triangulation earcut d'une surface plane AVEC TROUS (anneaux interieurs)."""
    import mapbox_earcut as earcut
    pts3 = np.array([p for ring in rings_pts for p in ring], dtype=np.float64)
    if len(pts3) < 3:
        return None, None
    outer = np.array(rings_pts[0], dtype=np.float64)
    m = len(outer)
    nrm = np.zeros(3)
    for i in range(m):
        a = outer[i]
        b = outer[(i + 1) % m]
        nrm[0] += (a[1] - b[1]) * (a[2] + b[2])
        nrm[1] += (a[2] - b[2]) * (a[0] + b[0])
        nrm[2] += (a[0] - b[0]) * (a[1] + b[1])
    L = np.linalg.norm(nrm)
    if L < 1e-9:
        return None, None
    nrm /= L
    ref = np.array([0.0, 0.0, 1.0]) if abs(nrm[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(nrm, ref)
    u /= np.linalg.norm(u)
    v = np.cross(nrm, u)
    p2 = np.ascontiguousarray(np.column_stack([pts3 @ u, pts3 @ v]))
    ring_ends = np.cumsum([len(r) for r in rings_pts]).astype(np.uint32)
    try:
        idx = earcut.triangulate_float64(p2, ring_ends)
    except Exception:
        return None, None
    if len(idx) < 3:
        return None, None
    return pts3, np.array(idx, dtype=np.int64).reshape(-1, 3)


def buildings_mesh(outdir):
    """CityJSONSeq roofer -> (V, F, nb features maillees).
    GOTCHA roofer : la geometrie est sur les CityObject ENFANTS (id '1-0'),
    les attributs sur le parent -> on parcourt TOUS les CityObjects."""
    Vall, Fall, off = [], [], 0
    n_feat = 0
    for fp in cityjson_files(outdir):
        lines = open(fp, encoding="utf-8").read().splitlines()
        if not lines:
            continue
        tr = json.loads(lines[0]).get("transform",
                                      {"scale": [1, 1, 1], "translate": [0, 0, 0]})
        sc = np.array(tr["scale"])
        tl = np.array(tr["translate"])
        for ln in lines[1:]:
            ln = ln.strip()
            if not ln:
                continue
            feat = json.loads(ln)
            if feat.get("type") != "CityJSONFeature":
                continue
            Vf = np.array(feat.get("vertices", []), dtype=np.float64)
            if len(Vf) == 0:
                continue
            n_feat += 1
            Vf = Vf * sc + tl
            for obj in feat.get("CityObjects", {}).values():
                geoms = obj.get("geometry", []) or []
                pref = [g for g in geoms if "2.2" in str(g.get("lod", ""))]
                for geom in (pref if pref else geoms):
                    gt = geom.get("type")
                    bnd = geom.get("boundaries", [])
                    surfaces = []
                    if gt == "Solid":
                        for shell in bnd:
                            surfaces += shell
                    elif gt in ("MultiSurface", "CompositeSurface"):
                        surfaces += bnd
                    elif gt == "CompositeSolid":
                        for solid in bnd:
                            for shell in solid:
                                surfaces += shell
                    for surf in surfaces:
                        rings = [[Vf[i] for i in ring] for ring in surf]
                        p3, tris = tri_surface(rings)
                        if p3 is None:
                            continue
                        Vall.append(p3)
                        Fall.append(tris + off)
                        off += len(p3)
    if not Vall:
        fail("aucune surface de batiment convertie depuis le CityJSON roofer")
    return np.vstack(Vall), np.vstack(Fall), n_feat


# ------------------------------------------------------ etape 8b : maillage sol
def ground_mesh(crop_laz, window, grid_m):
    """DTM classe 2, trous combles par plus-proche-voisin, median 3 + uniform 3
    (recette build_full_maquette). grid_m = pas adapte au cote de la fenetre
    (cote/500 m, plancher 1 m) pour garder ~250 000 mailles quel que soit le cote."""
    import laspy
    from scipy import ndimage

    minx, miny, maxx, maxy = window
    N = int(round((maxx - minx) / grid_m))
    Zf = np.full(N * N, -np.inf)
    n_sol = 0
    with laspy.open(crop_laz) as rd:   # lecture EN CHUNKS : le crop des grandes fenetres ne tient pas en RAM
        for pts in rd.chunk_iterator(4_000_000):
            cls = np.asarray(pts.classification)
            keep = cls == 2       # SOL NU seulement -> DTM propre
            if not keep.any():
                continue
            x = np.asarray(pts.x)[keep]
            y = np.asarray(pts.y)[keep]
            z = np.asarray(pts.z)[keep]
            n_sol += int(keep.sum())
            ix = np.clip(((x - minx) / grid_m).astype(np.int64), 0, N - 1)
            iy = np.clip(((y - miny) / grid_m).astype(np.int64), 0, N - 1)
            np.maximum.at(Zf, iy * N + ix, z)
    if n_sol == 0:
        fail("aucun point sol (classe 2) dans la fenetre, DTM impossible")
    Z = Zf.reshape(N, N)
    empty = ~np.isfinite(Z)
    if empty.any():       # rues etroites mal vues du ciel -> beaucoup de trous
        idx = ndimage.distance_transform_edt(empty, return_distances=False,
                                             return_indices=True)
        Z = Z[tuple(idx)]  # comble par le voisin le plus proche -> sol continu
    Z = ndimage.median_filter(Z, size=3)   # enleve les pics isoles
    Z = ndimage.uniform_filter(Z, size=3)  # lissage
    xs = minx + (np.arange(N) + 0.5) * grid_m
    ys = miny + (np.arange(N) + 0.5) * grid_m
    gx, gy = np.meshgrid(xs, ys)
    V = np.column_stack([gx.ravel(), gy.ravel(), Z.ravel()]).astype(np.float64)
    idx = np.arange(N * N).reshape(N, N)
    a = idx[:-1, :-1].ravel()
    b2 = idx[:-1, 1:].ravel()
    c = idx[1:, :-1].ravel()
    d = idx[1:, 1:].ravel()
    F = np.vstack([np.column_stack([a, b2, d]), np.column_stack([a, d, c])])
    return V, F, Z




# --------------------------------------------- etape 8c : arbres individuels
def trees_extract(crop_laz, window, Zdtm, dtm_grid_m):
    """Arbres INDIVIDUELS depuis les classes vegetation 3-5 : portage de l'algorithme
    navigateur valide (_uhiBuildLidarTrees) : canopee 1 m (lecture en chunks), cimes =
    maxima locaux 5x5 (seuil 2,5 m), houppier = distance ou la canopee retombe sous 25 %
    de la cime (moyenne 4 directions), suppression des jupes du plus haut au plus bas,
    plafond 5000 arbres (les plus hauts d'abord). Sortie : [x_rel, y_rel, z_sol, h, r]
    en metres, x/y RELATIFS AU CENTRE SNAPPE (meme repere que le GLB), z_sol ABSOLU."""
    import laspy
    from scipy import ndimage

    minx, miny, maxx, maxy = window
    N = int(round(maxx - minx))          # grille canopee fixe 1 m
    veg = np.full(N * N, -np.inf, dtype=np.float32)
    n_veg = 0
    with laspy.open(crop_laz) as rd:
        for pts in rd.chunk_iterator(4_000_000):
            cls = np.asarray(pts.classification)
            keep = (cls >= 3) & (cls <= 5)
            if not keep.any():
                continue
            x = np.asarray(pts.x)[keep]
            y = np.asarray(pts.y)[keep]
            z = np.asarray(pts.z)[keep]
            n_veg += int(keep.sum())
            ix = np.clip((x - minx).astype(np.int64), 0, N - 1)
            iy = np.clip((y - miny).astype(np.int64), 0, N - 1)
            np.maximum.at(veg, iy * N + ix, z.astype(np.float32))
    if n_veg == 0:
        return []
    veg = veg.reshape(N, N)
    # sol sous chaque cellule 1 m : plus proche voisin de la grille DTM (pas dtm_grid_m)
    Nd = Zdtm.shape[0]
    ii = np.clip(((np.arange(N) + 0.5) / dtm_grid_m).astype(np.int64), 0, Nd - 1)
    dtm1 = Zdtm[np.ix_(ii, ii)].astype(np.float32)
    chm = np.where(np.isfinite(veg), veg - dtm1, 0.0).astype(np.float32)
    chm[chm < 0] = 0.0
    MINH = 2.5
    mx = ndimage.maximum_filter(chm, size=5, mode="nearest")
    cand = np.argwhere((chm >= MINH) & (chm >= mx))   # [j, i]
    if not len(cand):
        return []
    order = np.argsort(-chm[cand[:, 0], cand[:, 1]])
    taken = np.zeros((N, N), dtype=bool)
    trees = []

    def rdir(gi, gj, di, dj, hp):
        r = 0
        for k in range(1, 13):
            p = gi + di * k
            q = gj + dj * k
            if p < 0 or p >= N or q < 0 or q >= N:
                break
            if chm[q, p] < 0.25 * hp:
                break
            r = k
        return r

    for oi in order:
        gj, gi = int(cand[oi, 0]), int(cand[oi, 1])
        if taken[gj, gi]:
            continue
        hp = float(chm[gj, gi])
        r4 = (rdir(gi, gj, 1, 0, hp) + rdir(gi, gj, -1, 0, hp)
              + rdir(gi, gj, 0, 1, hp) + rdir(gi, gj, 0, -1, hp)) / 4.0
        crown = max(1.5, min(9.0, r4 + 0.5))
        trees.append([round((gi + 0.5) - N / 2.0, 1), round((gj + 0.5) - N / 2.0, 1),
                      round(float(dtm1[gj, gi]), 1), round(hp, 1), round(crown, 1)])
        if len(trees) >= 6000:
            break
        rc = max(2, min(4, int(round(crown * 0.6))))   # plafonne : une haie dense garde ses cimes au lieu d'etre ecremee
        j0, j1 = max(0, gj - rc), min(N - 1, gj + rc)
        i0, i1 = max(0, gi - rc), min(N - 1, gi + rc)
        jj, iq = np.ogrid[j0:j1 + 1, i0:i1 + 1]
        taken[j0:j1 + 1, i0:i1 + 1] |= ((jj - gj) ** 2 + (iq - gi) ** 2 <= rc * rc)
    # REMPLISSAGE des masses continues (haies, boisements, ripisylves) : l'ecremage par cimes
    # eclaircit la canopee dense vue du ciel. Candidats = mailles libres avec chm >= 2,5 m tous
    # les ~3 m. Si les candidats DEBORDENT le budget (grandes fenetres forestieres), on en garde
    # un sur k mais on GONFLE les houppiers de sqrt(k) : la COUVERTURE de canopee est preservee
    # avec moins d'objets (plafond global 9000, leçon Haguenau 2,4 km plafonnee a 5000 clairsemes).
    cand2 = []
    for j in range(1, N - 1, 3):
        for i in range(1, N - 1, 3):
            if not taken[j, i] and chm[j, i] >= MINH:
                cand2.append((j, i))
    budget = max(0, 9000 - len(trees))
    if cand2 and budget:
        import math
        kk = max(1, int(math.ceil(len(cand2) / float(budget))))
        infl = math.sqrt(kk)
        for idx2 in range(0, len(cand2), kk):
            j, i = cand2[idx2]
            hp = float(chm[j, i])
            trees.append([round((i + 0.5) - N / 2.0, 1), round((j + 0.5) - N / 2.0, 1),
                          round(float(dtm1[j, i]), 1), round(hp, 1),
                          round(min(9.0, max(2.2, min(4.5, 0.42 * hp)) * infl), 1)])
            if len(trees) >= 9000:
                break
    return trees



# ------------------------------------------ etape 8d : grille physique du bati
def phys_rasterize(Vb, Fb, Zdtm, dtm_grid_m, window):
    """Rasterise les triangles LoD2.2 en grille 1 m pour la PHYSIQUE du simulateur :
    - H : hauteur du bati au-dessus du DTM (decimetres, uint16, 0 = pas de bati)
    - S : pente du pan gagnant en degres (uint8, 255 = pas de bati)
    - A : azimut du pan (sens de la descente, 0 = nord, est = 90) en demi-degres
          (uint8 0-179, 255 = pas de bati ou pan quasi plat < 5 deg)
    Le pan retenu par maille est celui qui donne le Z le plus HAUT (toit vu du ciel).
    Reperes : fenetre = centre snappe +/- cote/2, maille [j, i] = (y depuis miny,
    x depuis minx), row-major j * N + i, comme la canopee des arbres."""
    minx, miny, maxx, maxy = window
    N = int(round(maxx - minx))
    H = np.zeros(N * N, dtype=np.float32)          # hauteur ABSOLUE gagnante (avant conversion relative)
    H.fill(-np.inf)
    SL = np.full(N * N, 255, dtype=np.uint8)
    AZ = np.full(N * N, 255, dtype=np.uint8)
    tri = Vb[Fb]                                    # (nT, 3, 3) coordonnees absolues
    # normale de chaque triangle (orientee vers le haut)
    e1 = tri[:, 1] - tri[:, 0]
    e2 = tri[:, 2] - tri[:, 0]
    nrm = np.cross(e1, e2)
    flip = nrm[:, 2] < 0
    nrm[flip] *= -1.0
    ln = np.linalg.norm(nrm, axis=1)
    ok = ln > 1e-9
    for t in range(len(tri)):
        if not ok[t]:
            continue
        v = tri[t]
        i0 = max(0, int(np.floor(v[:, 0].min() - minx)))
        i1 = min(N - 1, int(np.floor(v[:, 0].max() - minx)))
        j0 = max(0, int(np.floor(v[:, 1].min() - miny)))
        j1 = min(N - 1, int(np.floor(v[:, 1].max() - miny)))
        if i1 < i0 or j1 < j0:
            continue
        xs = minx + np.arange(i0, i1 + 1) + 0.5
        ys = miny + np.arange(j0, j1 + 1) + 0.5
        gx, gy = np.meshgrid(xs, ys)
        # coordonnees barycentriques (2D)
        x1, y1 = v[0, 0], v[0, 1]
        x2, y2 = v[1, 0], v[1, 1]
        x3, y3 = v[2, 0], v[2, 1]
        det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if abs(det) < 1e-9:
            continue
        l1 = ((y2 - y3) * (gx - x3) + (x3 - x2) * (gy - y3)) / det
        l2 = ((y3 - y1) * (gx - x3) + (x1 - x3) * (gy - y3)) / det
        l3 = 1.0 - l1 - l2
        eps = -1e-6
        inside = (l1 >= eps) & (l2 >= eps) & (l3 >= eps)
        if not inside.any():
            continue
        z = l1 * v[0, 2] + l2 * v[1, 2] + l3 * v[2, 2]
        jj, ii = np.nonzero(inside)
        cells = (jj + j0) * N + (ii + i0)
        zc = z[jj, ii].astype(np.float32)
        upd = zc > H[cells]
        if not upd.any():
            continue
        cu = cells[upd]
        H[cu] = zc[upd]
        nx, ny, nz = nrm[t] / ln[t]
        slope = np.degrees(np.arccos(max(-1.0, min(1.0, nz))))
        SL[cu] = np.uint8(min(254, round(slope)))
        if slope >= 5.0:
            az = (np.degrees(np.arctan2(nx, ny)) + 360.0) % 360.0   # 0 = nord, est = 90
            AZ[cu] = np.uint8(min(179, int(az / 2.0)))
        else:
            AZ[cu] = 255
    # hauteur RELATIVE au DTM (grille dtm_grid_m -> 1 m par plus proche voisin)
    Nd = Zdtm.shape[0]
    iii = np.clip(((np.arange(N) + 0.5) / dtm_grid_m).astype(np.int64), 0, Nd - 1)
    dtm1 = Zdtm[np.ix_(iii, iii)].astype(np.float32).ravel()
    rel = H - dtm1
    bati = np.isfinite(H) & (rel >= 1.0)            # < 1 m au-dessus du sol = pas un bati
    Hdm = np.zeros(N * N, dtype=np.uint16)
    Hdm[bati] = np.clip(np.round(rel[bati] * 10.0), 1, 65535).astype(np.uint16)
    SL[~bati] = 255
    AZ[~bati] = 255
    return N, Hdm, SL, AZ, int(bati.sum())


def phys_write(path, N, side, Hdm, SL, AZ):
    """Binaire little-endian gzippe : magic VPH1, uint32 N, int32 cote_m,
    puis N*N uint16 hauteurs (dm), N*N uint8 pentes (deg), N*N uint8 azimuts (demi-deg)."""
    import struct as _st
    raw = b"VPH1" + _st.pack("<Ii", N, side) + Hdm.tobytes() + SL.tobytes() + AZ.tobytes()
    with gzip.open(path, "wb", compresslevel=7) as f:
        f.write(raw)
    return os.path.getsize(path)

# ---------------------------------------------- etape 9 : GLB + materiaux gris
def export_glb(Vb, Fb, Vg, Fg, snx, sny, out_glb):
    import trimesh
    Vb = Vb.copy()
    Vg = Vg.copy()
    Vb[:, 0] -= snx
    Vb[:, 1] -= sny   # repere local X/Y, Z = altitude absolue conservee
    Vg[:, 0] -= snx
    Vg[:, 1] -= sny
    scene = trimesh.Scene()
    scene.add_geometry(trimesh.Trimesh(vertices=Vb, faces=Fb, process=False),
                       geom_name="batiments", node_name="batiments")
    scene.add_geometry(trimesh.Trimesh(vertices=Vg, faces=Fg, process=False),
                       geom_name="sol", node_name="sol")
    scene.export(out_glb)
    fix_glb_materials(out_glb)
    return os.path.getsize(out_glb)


def fix_glb_materials(path):
    """Adapte de build_textured_maquette.fix_glb_materials : trimesh exporte
    metallicFactor absent (=>1.0 metal=sombre) -> on force dielectrique mat,
    et on COLORE par nom de mesh (batiments gris clair, sol gris moyen)."""
    b = open(path, "rb").read()
    jlen = struct.unpack("<I", b[12:16])[0]
    j = json.loads(b[20:20 + jlen].decode("utf-8"))
    rest = b[20 + jlen:]
    colors = {"batiments": COLOR_BATIMENTS, "sol": COLOR_SOL}
    mats = []
    mat_idx = {}

    def mat_for(name):
        if name not in mat_idx:
            mats.append({"name": name, "pbrMetallicRoughness": {
                "metallicFactor": 0.0, "roughnessFactor": 0.9,
                "baseColorFactor": colors[name]}})
            mat_idx[name] = len(mats) - 1
        return mat_idx[name]

    j["materials"] = mats   # on repart de zero : aucune texture dans ce GLB
    for me in j.get("meshes", []):
        name = (me.get("name") or "").lower()
        kind = "sol" if "sol" in name else "batiments"
        for p in me.get("primitives", []):
            p["material"] = mat_for(kind)
    nj = json.dumps(j).encode("utf-8")
    nj += b" " * ((4 - len(nj) % 4) % 4)
    out = bytearray(b[:12])
    out += struct.pack("<I", len(nj)) + b"JSON" + nj
    out += rest
    out[8:12] = struct.pack("<I", len(out))
    open(path, "wb").write(out)


# ------------------------------------------------------- etape 10 : index.json
def update_index(key, entry):
    data = {}
    if os.path.exists(INDEX):
        try:
            with open(INDEX, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            fail("index.json illisible (%s), correction manuelle requise" % e)
        if not isinstance(data, dict):
            fail("index.json n'est pas un objet JSON, correction manuelle requise")
    data[key] = entry
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    log("  index.json : %d cle(s)" % len(data))


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Maquette LiDAR HD -> GLB (Verdict)")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--label", type=str, default=None)
    ap.add_argument("--side", type=float, default=SIDE_DEFAUT,
                    help="cote de la fenetre en m (400 a 3200, arrondi a 100 ; defaut 500)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    t_all = time.perf_counter()

    # 1. geometrie du site
    side = min(3200.0, max(400.0, round(args.side / 100.0) * 100.0))
    with Chrono("1 geometrie"):
        cx, cy, snx, sny, key, window = site_geometry(args.lat, args.lon, side)
        label = args.label or key
        log("  centre L93 brut : %.2f, %.2f" % (cx, cy))
        log("  centre snappe 100 m : %d, %d -> cle %s (cote %d m)" % (snx, sny, key, int(side)))
        log("  fenetre : %.0f,%.0f -> %.0f,%.0f" % window)

    # 2. dalles intersectant la fenetre
    with Chrono("2 dalles WFS"):
        dalles = wfs_dalles(window)
        if not dalles:
            fail("aucune dalle LiDAR HD ne couvre la fenetre (zone non acquise ?)")
        log("  %d dalle(s) : %s" % (len(dalles), ", ".join(d["name"] for d in dalles)))

    # 3. dry-run : plan et sortie
    if args.dry_run:
        plan = {
            "key": key, "label": label,
            "lat": round(args.lat, 6), "lon": round(args.lon, 6),
            "centre_l93": [round(cx, 2), round(cy, 2)],
            "centre_snap": [snx, sny],
            "cote_m": int(side),
            "fenetre": list(window),
            "dalles": dalles,
        }
        print(json.dumps(plan, ensure_ascii=False))
        return 0

    for d in (DALLES_CACHE, WORK, GLB_DIR):
        os.makedirs(d, exist_ok=True)

    # 4. telechargement complet des dalles
    local_paths = []
    with Chrono("4 telechargement dalles"):
        for d in dalles:
            dest = os.path.join(DALLES_CACHE, d["name"] if d["name"].endswith(".laz")
                                else d["url"].split("/")[-1])
            download_dalle(d["url"], dest)
            local_paths.append(dest)

    # 5. crop local + fusion
    crop_laz = os.path.join(WORK, "crop.laz")
    with Chrono("5 crop + fusion"):
        n_points, n_ground = crop_and_merge(local_paths, window, crop_laz)

    # 6. emprises BDTOPO
    footprints = os.path.join(WORK, "footprints.geojson")
    with Chrono("6 emprises BDTOPO"):
        n_fp = fetch_footprints(window, footprints)
    if n_fp == 0:
        log("aucun batiment BDTOPO dans la fenetre, arret propre")
        sys.exit(3)

    # 7. roofer
    roofer_bin = os.environ.get("ROOFER_BIN",
                                os.path.join(BASE, "roofer", "bin", "roofer"))
    roofer_out = os.path.join(WORK, "roofer_out")
    with Chrono("7 roofer"):
        n_feat = run_roofer(roofer_bin, crop_laz, footprints, roofer_out,
                            os.path.join(WORK, "roofer_log.txt"))

    # 8. maillages
    with Chrono("8a maillage batiments"):
        Vb, Fb, n_meshed = buildings_mesh(roofer_out)
        log("  %d features maillees, %d sommets, %d triangles"
            % (n_meshed, len(Vb), len(Fb)))
    with Chrono("8b maillage sol"):
        Vg, Fg, Zg = ground_mesh(crop_laz, window, max(1.0, side / 500.0))
        log("  sol : %d sommets, %d triangles" % (len(Vg), len(Fg)))

    # 8c. arbres individuels (canopee LiDAR) -> glb/<cle>.trees.json
    with Chrono("8c arbres"):
        arbres = trees_extract(crop_laz, window, Zg, max(1.0, side / 500.0))
        trees_path = os.path.join(GLB_DIR, key + ".trees.json")
        with open(trees_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "cote_m": int(side), "n": len(arbres), "arbres": arbres},
                      f, separators=(",", ":"))
        log("  %d arbres -> %s" % (len(arbres), trees_path))

    # 8d. grille physique (hauteurs + normales de toit) -> glb/<cle>.phys.gz
    with Chrono("8d grille physique"):
        Np, Hdm, SLg, AZg, n_bati_cells = phys_rasterize(Vb, Fb, Zg, max(1.0, side / 500.0), window)
        phys_path = os.path.join(GLB_DIR, key + ".phys.gz")
        phys_size = phys_write(phys_path, Np, int(side), Hdm, SLg, AZg)
        log("  %d mailles baties / %d, %s : %.2f Mo"
            % (n_bati_cells, Np * Np, phys_path, phys_size / 1048576.0))

    # 9. export GLB + materiaux
    out_glb = os.path.join(GLB_DIR, key + ".glb")
    with Chrono("9 export GLB"):
        size = export_glb(Vb, Fb, Vg, Fg, snx, sny, out_glb)
        mo = round(size / 1048576.0, 2)
        log("  %s : %.2f Mo" % (out_glb, mo))

    # 10. index.json + resultat
    with Chrono("10 index.json"):
        entry = {
            "label": label,
            "cote_m": int(side),
            "arbres": len(arbres),
            "phys": True,
            "date": datetime.date.today().isoformat(),
            "lat": round(args.lat, 6),
            "lon": round(args.lon, 6),
            "batiments": n_meshed,
            "mo": mo,
        }
        update_index(key, entry)

    log("total pipeline : %.1f s" % (time.perf_counter() - t_all))
    result = {
        "key": key, "label": label,
        "lat": round(args.lat, 6), "lon": round(args.lon, 6),
        "centre_snap": [snx, sny],
        "cote_m": int(side),
        "dalles": [d["name"] for d in dalles],
        "points": n_points, "points_sol": n_ground,
        "batiments": n_meshed, "arbres": len(arbres), "mo": mo,
        "glb": os.path.relpath(out_glb, BASE).replace("\\", "/"),
        "url": RAW_URL % key,
    }
    print("MAQUETTE_RESULT " + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        fail("interrompu par l'utilisateur", 1)
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        fail("exception non geree : %s" % e, 1)
