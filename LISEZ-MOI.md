# IPTV Maghreb Auto — Algérie, Maroc, Tunisie

Une seule adresse M3U dans le lecteur IPTV et mise à jour automatisée quatre fois par jour, gratuitement via GitHub Actions.

## Contenu

- `maghreb.m3u` : sélection **filtrée** après contrôle HTTP depuis les serveurs GitHub (URL conseillée au quotidien).
- `maghreb-complet.m3u` : **toutes** les chaînes disponibles dans les listes IPTV-org de ces trois pays ; utile si un flux est bloqué pour GitHub mais fonctionne chez toi.
- `etat.json` : rapport du dernier contrôle (nombre de flux vus, flux accessibles, erreurs).
- `update_playlist.py` : le programme qui télécharge, classe, dédoublonne et teste les flux.
- `.github/workflows/refresh.yml` : le lancement automatique toutes les 6 h environ.
- `tests/test_parser.py` : tests locaux.

**Important :** les deux M3U incluses dans ce pack ne sont qu'une sélection de démarrage **non vérifiée** ; la première exécution de GitHub Actions reconstruira les listes depuis IPTV-org et effectuera les vérifications. Les liens peuvent être géobloqués, expirés, exiger un lecteur particulier ou tomber en panne après le test. Aucune solution gratuite ne garantit 100 % de disponibilité.

## Installer une seule fois (environ 5 étapes)

1. Crée un compte GitHub si tu n'en as pas : https://github.com/signup
2. Ouvre https://github.com/new, choisis le nom **iptv-maghreb-auto**, mets le dépôt sur **Public**, puis clique sur **Create repository**. Laisse le README automatique décoché si possible.
3. Décompresse ce pack sur ton PC. Dans le nouveau dépôt GitHub, choisis **uploading an existing file / Add file > Upload files**. Dépose les fichiers **et le dossier caché `.github`**, en conservant son chemin `.github/workflows/refresh.yml`. Si ton navigateur ne dépose pas le dossier caché, crée ce fichier via **Add file > Create new file** en saisissant le chemin complet, puis copie le contenu du fichier fourni. Valide avec **Commit changes**.
4. Ouvre l'onglet **Actions**, sélectionne **Actualiser IPTV Maghreb**, puis **Run workflow** sur la branche `main`. Laisse le traitement se terminer. Si l'onglet Actions est désactivé, autorise les actions dans les paramètres du dépôt. Le robot se relancera ensuite environ toutes les 6 h (GitHub peut décaler un déclenchement planifié).
5. Dans ton lecteur IPTV (VLC, TiviMate, IPTV Smarters...), ajoute cette adresse, en remplaçant `TON_PSEUDO` par ton identifiant GitHub exact :

   `https://raw.githubusercontent.com/TON_PSEUDO/iptv-maghreb-auto/main/maghreb.m3u`

   Pour la liste complète de secours :

   `https://raw.githubusercontent.com/TON_PSEUDO/iptv-maghreb-auto/main/maghreb-complet.m3u`

Active le **rafraîchissement automatique de la playlist** dans ton lecteur (toutes les 6 h ou au lancement, selon l'application). Sinon le lecteur peut conserver sa propre copie en cache même si le fichier GitHub est à jour.

## Où voir ce qui marche ?

Consulte ce rapport (après remplacement de `TON_PSEUDO`) :

`https://raw.githubusercontent.com/TON_PSEUDO/iptv-maghreb-auto/main/etat.json`

Le rapport indique les chaînes accessibles depuis le robot GitHub et celles dont la réponse a échoué. Ce **n'est pas** une garantie de lecture vidéo ou une vérification depuis ton réseau.

## Fonctionnement / limites

- Source : https://iptv-org.github.io/iptv/countries/dz.m3u (Algérie)
- Source : https://iptv-org.github.io/iptv/countries/ma.m3u (Maroc)
- Source : https://iptv-org.github.io/iptv/countries/tn.m3u (Tunisie)
- Contrôle par requête HTTP GET, et contrôle du format de base pour les manifestes HLS `.m3u8`.
- Une chaîne qui a répondu correctement pendant les dernières 24 heures peut rester dans la liste filtrée malgré un échec temporaire.
- Si l'une des listes source n'est pas accessible, le programme **s'arrête sans remplacer les fichiers existants** (aucune liste partielle ne remplace les trois pays).
- Si tous les tests de flux échouent, la dernière liste filtrée est conservée. La liste complète reste disponible.
- La liste complète et le rapport se reconstruisent à chaque exécution réussie.
- Dans les dépôts publics, GitHub peut désactiver automatiquement les exécutions planifiées après une longue période d'inactivité (généralement 60 jours). Un déclenchement manuel les réactive.
- Le projet ne contourne aucune restriction géographique ni aucun abonnement ; seules les URL publiques d'IPTV-org sont utilisées.

## Sans GitHub (la solution la plus simple)

Pour éviter toute installation, ajoute directement ces trois URL comme **trois playlists** dans le lecteur et active la mise à jour automatique du lecteur :

- `https://iptv-org.github.io/iptv/countries/dz.m3u`
- `https://iptv-org.github.io/iptv/countries/ma.m3u`
- `https://iptv-org.github.io/iptv/countries/tn.m3u`

C'est le plus simple, mais ces listes contiennent des liens non testés et ne sont pas regroupées en une seule adresse.
