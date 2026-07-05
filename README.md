# verdict-maquettes-lidar

Usine a maquettes 3D LoD2.2 a la demande pour Verdict.

A partir d'un simple couple latitude/longitude, une GitHub Action reconstruit une maquette 3D du site (batiments LoD2.2 + terrain) depuis le LiDAR HD de l'IGN et les emprises de la BD TOPO, puis publie le resultat au format GLB directement dans ce depot.

## But

Verdict (outil d'analyse de site et de simulation microclimatique) a besoin de maquettes 3D realistes de sites francais. Le nuage de points LiDAR brut est inexploitable dans un navigateur : ce depot fait la reconstruction hors ligne et sert des maillages legers, prets a charger.

Chaque maquette couvre une fenetre carree de 500 m (plus ou moins 250 m autour du centre) et contient deux maillages dans une scene GLB :

- `batiments` : toitures et murs LoD2.2 reconstruits par roofer (gris clair, sans texture) ;
- `sol` : terrain maille depuis le MNT LiDAR classe sol (gris moyen, sans texture).

Les sommets sont recentres en X/Y autour du centre de la fenetre ; le Z reste en altitude absolue (NGF).

## Architecture

- **L'Action GitHub est l'usine.** Le workflow `Maquette LiDAR` (`.github/workflows/maquette.yml`) telecharge les dalles LiDAR HD concernees, decoupe la fenetre, recupere les emprises BD TOPO, lance roofer, assemble le GLB et committe le resultat. Duree typique : quelques minutes par site, dans la limite de 45 minutes.
- **`glb/` est le cache eternel.** Chaque maquette produite est committee dans `glb/<cle>.glb` et indexee dans `index.json`. Une fois construite, une maquette n'est jamais recalculee : la cle etant deterministe, deux demandes proches du meme lieu retombent sur le meme fichier.
- **Trois declencheurs** pour lancer une construction :
  1. `workflow_dispatch` : lancement manuel depuis l'onglet Actions (voir plus bas) ;
  2. `repository_dispatch` (type `maquette`) : appel API avec un `client_payload` `{lat, lon, label}` ;
  3. `push` sur `main` d'un fichier `requests/*.json` : deposer un petit fichier JSON de demande suffit (voir `requests/EXEMPLE.json.txt`). Le fichier de demande est supprime par le bot une fois la maquette produite.

Les dalles LiDAR (~200 Mo chacune) ne sont jamais commitees ni mises en cache Actions : elles vivent et meurent avec le runner.

## Cout : zero

- Depot public : minutes GitHub Actions illimitees et stockage gratuit.
- Donnees IGN (LiDAR HD, BD TOPO, geocodage) : services publics gratuits sur data.geopf.fr.
- Diffusion des GLB : URL brute GitHub, sans serveur a maintenir.

## Lancer une maquette a la main

1. Ouvrir l'onglet **Actions** du depot.
2. Choisir le workflow **Maquette LiDAR** dans la liste de gauche.
3. Cliquer sur **Run workflow**, renseigner `lat` et `lon` (WGS84, ex. `45.77` / `4.83`) et un `label` facultatif.
4. Valider. Le resume du run affiche la cle, l'URL publique du GLB et les statistiques.

## Format de cle

Le centre demande est projete en Lambert 93 (EPSG:2154) puis **snappe a la grille de 100 m**. La cle est :

```
e<X_snappe>_n<Y_snappe>
```

avec X et Y entiers en metres. Exemple : `e842600_n6519100`. La fenetre de calcul est le carre de 500 m de cote centre sur ce point snappe, ce qui rend la cle deterministe et dedouble naturellement les demandes voisines.

URL publique d'une maquette :

```
https://raw.githubusercontent.com/romain800richardeau-dot/verdict-maquettes-lidar/main/glb/<cle>.glb
```

`index.json` recense toutes les maquettes disponibles : `{cle: {label, date, lat, lon, batiments, mo}}`.

## Licence

- **Donnees** : les maquettes GLB sont des oeuvres derivees du **LiDAR HD** et de la **BD TOPO** de l'IGN, diffusees sous **Licence Ouverte 2.0** (Etalab). Attribution requise : IGN.
- **Code** : le code de ce depot (pipeline, workflow, worker) est sous licence **MIT**.
