# RUNBOOK : deploiement du Worker de declenchement des maquettes LiDAR

Ce document decrit, pas a pas, comment mettre en service le Worker Cloudflare
(`worker/worker.js`). Le Worker est PREPARE mais PAS deploye : c'est vous qui
posez le jeton GitHub (PAT), personne d'autre ne doit le manipuler.

Rappel du role du Worker :

- `GET /maquette?lat=..&lon=..` : repond `ready` (avec l'URL du GLB) si la
  maquette existe deja sur le depot, `absent` sinon. Aucune ecriture.
- `POST /maquette` (JSON `{lat, lon, label}`) : si la maquette est absente,
  declenche le workflow GitHub Actions du depot `verdict-maquettes-lidar`
  via un `repository_dispatch` (event `maquette`). C'est la SEULE operation
  qui utilise le PAT.

## 1. Creer le PAT fine-grained (10 minutes)

Le jeton doit etre le plus restreint possible : un seul depot, une seule
permission.

1. Connectez-vous sur GitHub avec le compte `romain800richardeau-dot`.
2. Allez dans `Settings` > `Developer settings` >
   `Personal access tokens` > `Fine-grained tokens` > `Generate new token`.
   (URL directe : https://github.com/settings/personal-access-tokens/new)
3. Remplissez :
   - **Token name** : `verdict-maquette-worker` (ou tout nom parlant).
   - **Expiration** : `Custom` > 1 an (date du jour + 365 jours). Notez la
     date d'expiration dans votre agenda : le bouton Maquette cessera de
     fonctionner ce jour-la et il faudra regenerer un jeton.
   - **Resource owner** : `romain800richardeau-dot`.
   - **Repository access** : `Only select repositories` > cochez UNIQUEMENT
     `verdict-maquettes-lidar`.
   - **Permissions** > `Repository permissions` : mettez `Contents` sur
     `Read and write`. RIEN d'autre (toutes les autres lignes restent sur
     `No access`). C'est la permission minimale exigee par l'API
     `repository_dispatch`.
4. Cliquez `Generate token` et copiez le jeton (il commence par
   `github_pat_`). Il ne sera plus jamais affiche : gardez la page ouverte
   jusqu'a l'etape 3 ci-dessous.

## 2. Deployer le Worker

### Option A : ligne de commande (wrangler)

Prerequis : Node installe (c'est le cas sur votre machine), un compte
Cloudflare (le plan gratuit suffit tres largement).

Depuis le dossier `verdict-maquettes-lidar` :

```
npx wrangler login
npx wrangler deploy worker/worker.js --name verdict-maquette --compatibility-date 2026-07-01
```

- `wrangler login` ouvre le navigateur pour autoriser l'outil sur votre
  compte Cloudflare (une seule fois).
- `wrangler deploy` publie le Worker et affiche son URL, de la forme
  `https://verdict-maquette.<votre-sous-domaine>.workers.dev`.
  Notez cette URL : c'est elle que le bouton Maquette de Verdict appellera.

### Option B : dashboard Cloudflare (sans installer quoi que ce soit)

1. https://dash.cloudflare.com > `Workers & Pages` > `Create` >
   `Create Worker` > nommez-le `verdict-maquette` > `Deploy`.
2. `Edit code`, remplacez tout le contenu par celui de `worker/worker.js`,
   puis `Save and deploy`.

## 3. Poser le secret GH_PAT

Le jeton ne doit exister QUE comme secret Cloudflare. Jamais dans un fichier,
jamais dans un commit, jamais dans une conversation.

### Option A : wrangler

```
npx wrangler secret put GH_PAT --name verdict-maquette
```

La commande attend une saisie : collez le jeton puis validez par Entree.
Rien n'est ecrit sur le disque.

### Option B : dashboard

Worker `verdict-maquette` > `Settings` > `Variables and Secrets` >
`Add` > type `Secret` > nom `GH_PAT` > collez le jeton > `Deploy`.

Vous pouvez maintenant fermer la page GitHub du jeton.

## 4. Tester (curl)

Remplacez `WORKER` par l'URL notee a l'etape 2.

Test 1, lecture seule (aucun PAT utilise) :

```
curl "https://WORKER/maquette?lat=45.695&lon=-0.329"
```

Reponse attendue : `{"status":"absent","key":"e4XXXXX_n64XXXXX"}` si la
maquette n'existe pas encore, ou `{"status":"ready","key":...,"url":...}`
si elle a deja ete construite.

Test 2, coordonnees hors France (le garde-fou doit refuser) :

```
curl "https://WORKER/maquette?lat=35.0&lon=25.0"
```

Reponse attendue : HTTP 400, message `coordonnees hors France metropolitaine`.

Test 3, declenchement reel (consomme un run GitHub Actions) :

```
curl -X POST "https://WORKER/maquette" -H "Content-Type: application/json" -d "{\"lat\":45.695,\"lon\":-0.329,\"label\":\"Cognac test\"}"
```

Reponse attendue : `{"status":"building","key":...,"checkUrl":...}`.
Verifiez ensuite dans l'onglet `Actions` du depot GitHub qu'un run
`maquette` est bien parti, puis interrogez `checkUrl` (le meme GET que le
test 1) jusqu'a obtenir `ready` (comptez 5 a 15 minutes, dalles LiDAR
entieres a telecharger).

Si le POST repond `503 GH_PAT non configure`, le secret n'est pas pose
(etape 3). S'il repond `502` avec un detail GitHub `Resource not accessible`,
le PAT n'a pas la permission `Contents Read and write` ou ne couvre pas le
bon depot : regenerez-le proprement.

## 5. Avertissements

- Ne collez JAMAIS le PAT ailleurs que dans le secret Cloudflare : pas dans
  `worker.js`, pas dans le code de Verdict, pas dans un fichier du depot,
  pas dans une conversation avec une IA. Le Worker est le seul a le voir,
  cote serveur, via `env.GH_PAT`.
- Si vous soupconnez une fuite du jeton, revoquez-le immediatement sur
  GitHub (`Settings` > `Developer settings` > `Fine-grained tokens` >
  `Revoke`), puis regenerez-en un et remettez a jour le secret (etape 3).
  Le perimetre etant limite a `Contents` sur ce seul depot, le pire cas
  d'une fuite reste borne : ecriture sur `verdict-maquettes-lidar` uniquement.
- L'URL du Worker est publique et sans authentification : n'importe qui la
  connaissant peut declencher des builds. Les garde-fous integres (bornes
  France metropolitaine, deduplication par cle de site, 429 en cas de rate
  limit GitHub) limitent l'abus, mais si un jour le trafic devient anormal,
  ajoutez une regle de rate limiting Cloudflare (dashboard > Security) ou
  un en-tete secret partage avec Verdict.
- Le PAT expire au bout d'un an : a l'expiration, seul le POST (construction
  de nouvelles maquettes) casse ; les maquettes existantes restent servies.
