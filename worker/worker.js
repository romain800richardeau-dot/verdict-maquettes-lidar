/*
 * Worker Cloudflare : declenchement des maquettes LiDAR Verdict.
 *
 * PREPARE mais PAS deploye. Le secret GH_PAT est pose par Romain via
 * `wrangler secret put GH_PAT` (voir RUNBOOK.md). Le PAT n'apparait
 * JAMAIS dans ce fichier ni dans aucun depot.
 *
 * Routes :
 *   GET  /maquette?lat=..&lon=..   -> {status:'ready', key, url} si le GLB existe,
 *                                     sinon {status:'absent', key}
 *   POST /maquette {lat, lon, label} -> declenche un repository_dispatch GitHub
 *                                     (event_type 'maquette') si le GLB est absent
 *                                     -> {status:'building', key, checkUrl}
 *   OPTIONS *                      -> preflight CORS
 *
 * Contrat de cle (identique a pipeline/pipeline.py) :
 *   centre L93 snappe a la grille 100 m (arrondi au plus proche),
 *   key = 'e' + X_snappe + '_n' + Y_snappe (entiers).
 */

const REPO = 'romain800richardeau-dot/verdict-maquettes-lidar';
const RAW_GLB_BASE = 'https://raw.githubusercontent.com/' + REPO + '/main/glb/';
const DISPATCH_URL = 'https://api.github.com/repos/' + REPO + '/dispatches';

// Bornes France metropolitaine (garde-fou du contrat).
const LAT_MIN = 41.0;
const LAT_MAX = 51.5;
const LON_MIN = -5.5;
const LON_MAX = 10.0;

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Access-Control-Max-Age': '86400',
};

/*
 * Projection WGS84 (EPSG:4326) -> Lambert 93 (EPSG:2154) en JS pur.
 * Lambert Conforme Conique 2 paralleles, formules officielles IGN
 * (note NT/G 71, methode EPSG 9802), ellipsoide GRS80.
 * RGF93 est confondu avec WGS84 (ecart reel < 10 cm), largement
 * suffisant pour un snap a 100 m (precision verifiee < 1 mm vs pyproj).
 * Exportee pour le harnais de test Node (sans effet cote Cloudflare).
 */
export function wgs84ToLambert93(lat, lon) {
  var a = 6378137.0;                 // demi-grand axe GRS80 (m)
  var e = 0.0818191910428158;        // premiere excentricite GRS80
  var d2r = Math.PI / 180;
  var phi = lat * d2r;
  var lam = lon * d2r;
  var phi0 = 46.5 * d2r;             // latitude d'origine
  var phi1 = 44.0 * d2r;             // 1er parallele automecoique
  var phi2 = 49.0 * d2r;             // 2e parallele automecoique
  var lam0 = 3.0 * d2r;              // meridien central
  var x0 = 700000.0;                 // false easting
  var y0 = 6600000.0;                // false northing

  function bigM(p) { // rayon reduit du parallele
    var s = Math.sin(p);
    return Math.cos(p) / Math.sqrt(1 - e * e * s * s);
  }
  function bigT(p) { // latitude isometrique exponentiee : exp(-L(phi))
    var s = Math.sin(p);
    return Math.tan(Math.PI / 4 - p / 2) /
      Math.pow((1 - e * s) / (1 + e * s), e / 2);
  }

  var n = (Math.log(bigM(phi1)) - Math.log(bigM(phi2))) /
          (Math.log(bigT(phi1)) - Math.log(bigT(phi2)));
  var F = bigM(phi1) / (n * Math.pow(bigT(phi1), n));
  var rho0 = a * F * Math.pow(bigT(phi0), n);
  var rho = a * F * Math.pow(bigT(phi), n);
  var theta = n * (lam - lam0);

  return {
    x: x0 + rho * Math.sin(theta),
    y: y0 + rho0 - rho * Math.cos(theta),
  };
}

/* Cle de site : snap du centre L93 a la grille 100 m (meme formule que le pipeline).
   side != 500 -> suffixe _w<cote> (les cles 500 m historiques restent sans suffixe). */
export function normSide(raw) {
  var side = Number(raw);
  if (!isFinite(side) || side <= 0) side = 500;
  side = Math.round(side / 100) * 100;
  return Math.min(6000, Math.max(400, side));
}
export function siteKey(lat, lon, side) {
  var p = wgs84ToLambert93(lat, lon);
  var xs = Math.round(p.x / 100) * 100;
  var ys = Math.round(p.y / 100) * 100;
  var key = 'e' + xs + '_n' + ys;
  var sd = normSide(side);
  return sd === 500 ? key : key + '_w' + sd;
}

function jsonResponse(obj, status) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: Object.assign(
      { 'Content-Type': 'application/json; charset=utf-8' },
      CORS_HEADERS
    ),
  });
}

function parseCoords(latRaw, lonRaw) {
  var lat = Number(latRaw);
  var lon = Number(lonRaw);
  if (!isFinite(lat) || !isFinite(lon)) {
    return { error: 'lat et lon doivent etre des nombres' };
  }
  if (lat < LAT_MIN || lat > LAT_MAX || lon < LON_MIN || lon > LON_MAX) {
    return {
      error: 'coordonnees hors France metropolitaine (lat ' + LAT_MIN + '..' +
        LAT_MAX + ', lon ' + LON_MIN + '..' + LON_MAX + ')',
    };
  }
  return { lat: lat, lon: lon };
}

/* HEAD sur le raw GitHub, avec cache-buster pour contourner le cache CDN (~5 min). */
async function glbExists(key) {
  var url = RAW_GLB_BASE + key + '.glb';
  var resp = await fetch(url + '?t=' + Date.now(), { method: 'HEAD' });
  return { exists: resp.status === 200, url: url };
}

async function handleGet(url) {
  var c = parseCoords(url.searchParams.get('lat'), url.searchParams.get('lon'));
  if (c.error) return jsonResponse({ status: 'error', message: c.error }, 400);

  var key = siteKey(c.lat, c.lon, url.searchParams.get('side'));
  var probe = await glbExists(key);
  if (probe.exists) {
    return jsonResponse({ status: 'ready', key: key, url: probe.url });
  }
  return jsonResponse({ status: 'absent', key: key });
}

async function handlePost(request, url, env) {
  var body;
  try {
    body = await request.json();
  } catch (e) {
    return jsonResponse({ status: 'error', message: 'corps JSON invalide' }, 400);
  }

  var c = parseCoords(body && body.lat, body && body.lon);
  if (c.error) return jsonResponse({ status: 'error', message: c.error }, 400);

  var label = typeof (body && body.label) === 'string'
    ? body.label.slice(0, 120)
    : '';

  var side = normSide(body && body.side);
  var key = siteKey(c.lat, c.lon, side);
  var probe = await glbExists(key);
  if (probe.exists) {
    return jsonResponse({ status: 'ready', key: key, url: probe.url });
  }

  if (!env || !env.GH_PAT) {
    return jsonResponse(
      { status: 'error', message: 'GH_PAT non configure sur le Worker' },
      503
    );
  }

  var ghResp = await fetch(DISPATCH_URL, {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + env.GH_PAT,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'verdict-maquette-worker',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      event_type: 'maquette',
      client_payload: { lat: c.lat, lon: c.lon, label: label, side: side },
    }),
  });

  if (ghResp.status === 204) {
    var checkUrl = url.origin + '/maquette?lat=' + encodeURIComponent(c.lat) +
      '&lon=' + encodeURIComponent(c.lon) + '&side=' + side;
    return jsonResponse({ status: 'building', key: key, checkUrl: checkUrl });
  }

  var ghText = '';
  try { ghText = await ghResp.text(); } catch (e) { /* sans objet */ }

  if (ghResp.status === 403 &&
      (ghResp.headers.get('x-ratelimit-remaining') === '0' ||
       /rate limit/i.test(ghText))) {
    return jsonResponse(
      { status: 'error', message: 'API GitHub en rate limit, reessayez plus tard' },
      429
    );
  }

  return jsonResponse(
    {
      status: 'error',
      message: 'echec du declenchement GitHub (HTTP ' + ghResp.status + ')',
      detail: ghText.slice(0, 300),
    },
    502
  );
}

export default {
  async fetch(request, env) {
    var url = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    if (url.pathname === '/maquette') {
      if (request.method === 'GET') return handleGet(url);
      if (request.method === 'POST') return handlePost(request, url, env);
      return jsonResponse(
        { status: 'error', message: 'methode non autorisee' },
        405
      );
    }

    return jsonResponse({ status: 'error', message: 'route inconnue' }, 404);
  },
};
