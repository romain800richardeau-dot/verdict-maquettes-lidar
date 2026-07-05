/*
 * SNIPPET DE REFERENCE, PAS BRANCHE.
 *
 * Montre comment le futur bouton "Maquette" de Verdict (index.html,
 * volet Microclimat) appellera le Worker Cloudflare une fois celui-ci
 * deploye (voir RUNBOOK.md). A integrer a la main dans UHI_SIM.js /
 * index.html quand Romain aura valide le deploiement ; ne rien coller
 * automatiquement.
 *
 * Flux :
 *   1. POST /maquette {lat, lon, label}
 *      -> 'ready'    : le GLB existe deja, on le charge tout de suite ;
 *      -> 'building' : le workflow GitHub Actions est parti, on poll ;
 *      -> erreur     : on informe l'utilisateur et on s'arrete.
 *   2. Poll GET /maquette?lat=..&lon=.. toutes les 30 s, 15 min max
 *      (30 tentatives). Le build reel prend 5 a 15 min (dalles LiDAR
 *      entieres, 160 a 230 Mo chacune).
 *   3. Des que 'ready' : chargement via la passerelle existante
 *      window._saUhiSimLoadMesh(url + '?t=' + Date.now())
 *      (le cache-buster contourne le cache CDN de raw.githubusercontent,
 *      Cache-Control max-age=300).
 */

/* URL du Worker une fois deploye (etape 2 du RUNBOOK). */
var VERDICT_MAQUETTE_WORKER = 'https://verdict-maquette.VOTRE-SOUS-DOMAINE.workers.dev/maquette';

var VERDICT_MAQUETTE_POLL_MS = 30 * 1000;      /* 30 s entre deux GET */
var VERDICT_MAQUETTE_POLL_MAX = 30;            /* 30 x 30 s = 15 min max */

/*
 * Demande (ou recupere) la maquette LiDAR du site courant.
 *
 * @param {number} lat        latitude WGS84 du centre du site
 * @param {number} lon        longitude WGS84 du centre du site
 * @param {string} label      libelle humain (adresse), transmis au pipeline
 * @param {function} onStatus callback de progression pour l'UI,
 *                            recoit une string en francais (vouvoiement)
 */
async function saRequestMaquetteLidar(lat, lon, label, onStatus) {
  var notify = onStatus || function () {};

  /* --- 1. POST : declenche le build si necessaire --- */
  var resp = await fetch(VERDICT_MAQUETTE_WORKER, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lat: lat, lon: lon, label: label || '' }),
  });
  var data = await resp.json();

  if (!resp.ok) {
    /* 400 hors bornes, 429 rate limit GitHub, 502/503 config Worker */
    notify('Maquette indisponible : ' + (data.message || 'HTTP ' + resp.status));
    return null;
  }

  if (data.status === 'ready') {
    notify('Maquette deja disponible, chargement...');
    return saLoadMaquetteGlb(data.url);
  }

  /* --- 2. status 'building' : poll toutes les 30 s, 15 min max --- */
  notify('Construction de la maquette lancee (5 a 15 min). ' +
    'Vous pouvez continuer a utiliser Verdict pendant ce temps.');

  var checkUrl = data.checkUrl ||
    (VERDICT_MAQUETTE_WORKER + '?lat=' + lat + '&lon=' + lon);

  for (var attempt = 1; attempt <= VERDICT_MAQUETTE_POLL_MAX; attempt++) {
    await new Promise(function (ok) { setTimeout(ok, VERDICT_MAQUETTE_POLL_MS); });

    try {
      var poll = await fetch(checkUrl);
      var st = await poll.json();
      if (st.status === 'ready') {
        notify('Maquette prete, chargement...');
        return saLoadMaquetteGlb(st.url);
      }
      notify('Construction en cours... (' + attempt + '/' +
        VERDICT_MAQUETTE_POLL_MAX + ')');
    } catch (e) {
      /* erreur reseau transitoire : on retente au tick suivant */
    }
  }

  notify('La construction depasse 15 min. Reessayez plus tard : ' +
    'la maquette sera servie instantanement une fois terminee.');
  return null;
}

/*
 * Chargement du GLB dans la 3D Microclimat via la passerelle existante
 * (_saUhiSimLoadMesh, cf. module "Maquette LiDAR precalculee").
 * Le '?t=' force raw.githubusercontent a servir la version fraiche.
 */
function saLoadMaquetteGlb(url) {
  var freshUrl = url + '?t=' + Date.now();
  if (typeof window._saUhiSimLoadMesh === 'function') {
    window._saUhiSimLoadMesh(freshUrl);
    return freshUrl;
  }
  /* La simu 3D n'est pas encore initialisee : a brancher selon le contexte */
  console.warn('maquette prete mais _saUhiSimLoadMesh absent :', freshUrl);
  return freshUrl;
}

/*
 * Exemple d'appel depuis le futur bouton (les variables lat/lon/label
 * viennent de l'etat du site courant, cf. _saUhiSimState) :
 *
 *   saRequestMaquetteLidar(45.695, -0.329, '12 rue Exemple, Cognac',
 *     function (msg) { monBandeauStatut.textContent = msg; });
 */
