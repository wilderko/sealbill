# 1. Prise en main

## Ce que vous exploitez

Sealbill se compose d'une application web et d'un worker en arrière-plan.

- L'**application web** sert les pages du personnel (à la racine du site, par exemple `https://pay.example.org/`), les pages du client (`/i/…` affiche une facture, `/p/…` est l'endroit où l'on paie, `/l/…` est un lien de paiement), l'API (`/api/v1/…`) et la page de déverrouillage (`/unlock/`).
- Le **worker** (`manage.py run_worker`) fait tout ce qui doit se produire sans clic : il envoie les e-mails en file d'attente, marque les factures en retard, surveille les blockchains et les passerelles pour détecter les paiements, fait expirer les devis de change, envoie l'unique rappel de paiement et livre les webhooks. **Sans le worker, aucun e-mail n'est envoyé et aucun paiement crypto n'est remarqué.** Lancez-le comme un second processus à côté de l'application web ; `manage.py run_worker --once` exécute chaque tâche une fois puis s'arrête, ce qui est pratique pour un test.

## Installation et premier démarrage

Le README du dépôt contient les commandes pour chaque mode d'exploitation, elles ne sont donc pas répétées ici :

- sur votre propre machine, pour essayer : [README, Development](../../../README.md#development)
- avec Docker Compose : [README, Docker Compose](../../../README.md#docker-compose)
- sur un serveur avec systemd, Apache et PostgreSQL : [README, Deploying with systemd and Apache](../../../README.md#deploying-with-systemd-and-apache)

Quel que soit votre choix, l'ordre du premier démarrage est le même :

1. Créez les tables de la base de données (`migrate`, et `createcachetable` pour les tables de limitation de débit).
2. Créez le coffre et déverrouillez-le (section suivante).
3. Créez le premier utilisateur avec `manage.py createsuperuser`.
4. Donnez-lui un second facteur : `manage.py create_staff_totp <username>` affiche une adresse `otpauth://` pour une application d'authentification. Le coffre doit être déverrouillé. Vous pourrez ajouter une passkey plus tard sur la page Security.
5. Démarrez l'application web et le worker, ouvrez le site et connectez-vous.
6. Dans **Settings**, créez une organisation et un compte bancaire ou un compte de canal de paiement (chapitre 2), puis envoyez-vous une première facture (chapitre 4).

## Le coffre

Tout ce qu'une personne a saisi (noms, adresses, identifiants fiscaux, lignes de facture, notes, identifiants de vos passerelles de paiement) est stocké chiffré. La clé est dérivée d'un **mot de passe du coffre** et n'est conservée que dans le fichier en mémoire désigné par `VAULT_KEY_PATH` ; elle n'est jamais dans l'environnement ni sur le disque.

```bash
printf '%s\n' 'a long vault password' | manage.py init_vault      # une fois ; au moins 16 caractères
printf '%s\n' 'a long vault password' | manage.py unseal_vault    # après chaque redémarrage de la machine
```

Saisissez le mot de passe sur l'entrée standard, comme ci-dessus, pour qu'il n'apparaisse jamais dans une ligne de commande ni dans l'historique du shell. `init_vault` ne fait que créer `vault.json` (le sel et une valeur de contrôle, pas la clé) ; `unseal_vault` l'ouvre.

Après un redémarrage de la machine, ou si le fichier en mémoire est effacé, le coffre est **scellé**. Un rechargement en douceur du service web le laisse ouvert ; un redémarrage brutal, non. Tant qu'il est scellé :

- toute page du personnel redirige vers **/unlock/** : saisissez-y le mot de passe du coffre ou lancez `unseal_vault` ;
- les pages de paiement, les liens de paiement et l'API répondent « temporairement indisponible » (503), si bien qu'aucun client ne peut obtenir de devis de change ;
- le worker continue de voir et de rapprocher les paiements sur la blockchain (il ne lit que des colonnes non chiffrées), mais les e-mails et les webhooks attendent et l'interrogation des passerelles (BTCPay, Lightning) est suspendue ; l'exploitant reçoit un e-mail d'alerte par épisode de scellement.

![La page de déverrouillage, affichée quand le coffre est scellé](../img/vault-unlock.png)

`GET /healthz` indique `"vault": "sealed"` ou `"unsealed"` sans aucune donnée, un outil de supervision peut donc le surveiller. Conservez une copie de `vault.json`, la phrase de passe de sauvegarde et le mot de passe du coffre ailleurs que sur le serveur : une base de données sans les trois est illisible. La liste complète de ce qui continue de fonctionner coffre scellé est dans le [README](../../../README.md#what-runs-while-the-vault-is-sealed).

## Se connecter

Le personnel se connecte sur `/login/` en deux étapes : le **mot de passe**, puis un **second facteur**, qui est une passkey ou un code à six chiffres d'une application d'authentification. Une session dure huit heures et prend fin à la fermeture du navigateur. La connexion est limitée en débit par adresse, par compte et par appareil.

![L'étape du mot de passe](../img/start-login.png)

![L'étape du second facteur](../img/start-verify.png)

Quand le compte a une passkey, la seconde étape propose d'abord **Use a passkey**, puis le champ du code en dessous (« or enter a code »). Une passkey n'appartient qu'à ce site, un site imitateur ne peut donc pas l'utiliser.

![La seconde étape d'un compte qui a une passkey](../img/verify-passkey.png)

Après la connexion, vous arrivez sur **Needs a person** (chapitre 6). La barre de gauche constitue toute la navigation : Needs a person, Invoices, New invoice, Contacts, Payment links, Exports, Settings ; le bouclier en bas est **Security**, la flèche vous déconnecte.

![Needs a person, la première page après la connexion](../img/needs-person.png)

### Security : vos propres passkeys et authentificateurs

**Security** (l'icône de bouclier) liste vos seconds facteurs. Ajoutez une passkey en lui donnant un nom (par exemple « MacBook Touch ID » ou « YubiKey 5C ») et en appuyant sur **Add a passkey** ; le navigateur demande alors l'empreinte, le visage ou le code PIN. Une application d'authentification s'ajoute sur le serveur avec `create_staff_totp`, jamais dans le navigateur. Le dernier second facteur d'un compte ne peut pas être supprimé, et supprimer celui avec lequel la session s'est connectée en demande d'abord un autre. Chaque ajout et chaque suppression figurent dans le journal d'audit.

![Security, avec une passkey ajoutée](../img/security-passkey.png)

## Personnel et super-utilisateurs

Tout compte du personnel peut émettre des factures, enregistrer des paiements et traiter les files. Certains changements peuvent déplacer de l'argent ou rediriger un client, et demandent davantage.

### La reconfirmation (step-up)

Ces changements exigent un **super-utilisateur** qui a donné un second facteur **au cours des dix dernières minutes** :

- créer, redéfinir les scopes ou révoquer un jeton d'API ;
- un compte de canal de paiement et ses identifiants (et le webhook BTCPay, qui stocke un secret) ;
- l'indicateur d'émetteur pour résidents fiscaux américains d'une organisation et ses hôtes de retour ;
- un compte bancaire ou un lien de paiement fiat d'une organisation ;
- ajouter une adresse e-mail à un contact ou en faire la principale (les e-mails, les liens de paiement et les PDF y vont) ;
- supprimer le second facteur d'un autre membre du personnel.

Les pages vous disent où vous en êtes avant que vous n'appuyiez sur quoi que ce soit :

- un membre du personnel qui n'est pas super-utilisateur voit « Only a superuser may … » et rien ne change ;

  ![Un membre du personnel qui n'est pas super-utilisateur](../img/stepup-not-superuser.png)

- un super-utilisateur dont le dernier second facteur date de plus de dix minutes voit **Verify again** ; il mène à la page de vérification et vous ramène à la page où vous étiez.

  ![Un changement qui demande un second facteur récent](../img/stepup-reverify.png)

  ![Verify again](../img/stepup-verify.png)

Chaque changement soumis à reconfirmation envoie aussi un e-mail à chaque super-utilisateur actif : qui, quoi et quand, par noms et identifiants, jamais un secret, un numéro de compte ou une adresse. Si une session volée modifie quelque chose, le propriétaire du système le sait.

La page **Team** (Settings > Team) liste toutes les personnes qui peuvent se connecter, leurs seconds facteurs et leur dernière connexion, et permet à un super-utilisateur de supprimer le facteur perdu d'un collègue. Les nouveaux membres du personnel se créent avec `createsuperuser` ou dans l'admin Django masqué (le chemin de `DJANGO_ADMIN_PATH` ; super-utilisateur avec second facteur uniquement, tous les autres reçoivent un 404).
