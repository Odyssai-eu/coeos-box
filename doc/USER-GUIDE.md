# coeos-box — guide de la box

> La box de CoeOS : ce qu'elle fait, comment l'installer seule, la brancher sur vos
> modèles (locaux et cloud), l'utiliser depuis vos outils, et ce qu'elle vous
> garantit. Le produit complet (client inclus) est [Odyssai-eu/coeos](https://github.com/Odyssai-eu/coeos). Tout ici
> décrit le code tel qu'il est (`src/coeos/`), pas une feuille de route.

---

## 1. En une phrase

CoeOS est une **passerelle** : vos outils lui parlent comme à un seul modèle
(`CoeOS`), et elle envoie chaque requête au modèle **prouvé meilleur sur la
compétence demandée** — avec vos clés, chez vous. Elle ne fait pas d'inférence
elle-même.

```
requête ──▶ 1. quelle compétence ?  (en-tête x-coeos-axis, sinon décideur, sinon axe par défaut)
            2. quel modèle est le meilleur dessus ?  (les TMB Settings le disent)
            3. relais au provider qui l'héberge  (OdyssAI-X local, OpenRouter, le vôtre)
◀── réponse + en-têtes x-coeos-axis / x-coeos-model / x-coeos-provider
```

## 2. Les trois concepts

| Terme | Ce que c'est |
|---|---|
| **Axe de compétence** | Une catégorie *benchmarkée*. 18 axes livrés : `creative`, `redac_pro`, `legal_rgpd`, `legal_complex`, `reasoning`, `calc`, `python`, `code_general`, `debug`, `react`, `swift`, `refactoring`, `plan_decompo`, `plan_spec`, `plan_judgment`, `fast_tools`, `agent_exec`, `agent_safety`. |
| **TMB Settings** | Le fichier qui dit, par axe, **quel modèle servir** — et le régime (`cloud` / `local`), l'axe par défaut, le décideur. C'est la « recette ». Un fichier livré : `TMB Settings — 100 % Cloud (best-of-all)`. Vous pouvez écrire le vôtre. |
| **Provider** | Qui héberge un modèle : `openrouter` (cloud, clé requise), `odyssai` (**local**, OpenAI-compatible, **sans clé** — un moteur OdyssAI-X sur votre LAN), ou tout provider que vous déclarez (`api_base` + clé). |

Comment l'axe est choisi, dans l'ordre :
1. **En-tête explicite** `x-coeos-axis: <axe>` — l'agent sait ce qu'il fait, CoeOS obéit.
2. **Décideur** — si un `decider_model` est configuré dans les settings, ce petit modèle classe la requête.
3. **Axe par défaut** (`default_axis`, `code_general` dans le fichier livré) — sinon.

Un id de modèle explicite est aussi accepté à la place de `CoeOS` :
`or:<org/modele>` (OpenRouter) ou `local:<modele>` (votre moteur).

## 3. Installer

Prérequis : Docker, ou Python ≥ 3.10.

```bash
git clone https://github.com/Odyssai-eu/coeos-box.git && cd coeos-box
docker compose up -d --build            # box sur :4600, console aussi sur :4800
curl -s http://localhost:4600/health     # {"status": …}
```

Sans Docker : `pip install . && coeos-box` (`--host`, `--port`, `--console-port`).

La configuration vit dans `coeos-config.json` (`COEOS_CONFIG` ; en Docker
`/data/coeos-config.json`, volume `./data`). Elle contient les clés provider
**chiffrées** (Fernet) ; la clé maître est `COEOS_MASTER_KEY` ou le fichier
`coeos-master.key` (0600) — gardez-le hors de l'image et hors de git.

## 4. Sécuriser : la première clé

Tant qu'**aucune** clé n'existe, l'instance est ouverte (pratique sur `localhost`).
Dès qu'une clé est émise, `/v1/*` et l'administration exigent un token. **La
première clé créée est admin.**

```bash
python -m coeos.accounts create-user alice --mode byok   # byok : ses propres clés provider
python -m coeos.accounts issue-key alice --name laptop   # affiche ck_… UNE fois
python -m coeos.accounts list
python -m coeos.accounts revoke ck_1234
```

Modes de compte : `byok` (le compte apporte ses clés provider) ou plateforme
(il utilise la clé de l'opérateur). Un compte n'obtient **jamais** la clé
globale de l'opérateur en repli. Quotas par compte : tokens/jour et
requêtes/minute (`COEOS_DAY_TOKENS_DEFAULT`, `COEOS_RPM_DEFAULT`), comptés
localement (onglet *Usage*, `GET /v1/usage`).

## 5. Brancher les modèles

### 5.1 Un moteur local (OdyssAI-X)

Le provider `odyssai` est livré, vide et sans clé — l'adresse est la vôtre :

```bash
curl -X PUT http://localhost:4600/admin/providers/odyssai \
  -H 'content-type: application/json' \
  -d '{"api_base":"http://<votre-moteur>:8000/v1"}'
```

(Ou console → *Providers*.) CoeOS parle à OdyssAI-X avec le bon drapeau de
raisonnement (`enable_thinking`, le seul qu'il écoute), traduit `/v1/messages`
si votre outil est Anthropic, et retire les champs internes avant relais.

### 5.2 Le cloud

- **OpenRouter** : clé dans la console (*Providers*) ou `OPENROUTER_API_KEY` au
  premier démarrage.
- **Un autre provider OpenAI-compatible** :

```bash
curl -X POST http://localhost:4600/admin/platform/providers \
  -H 'content-type: application/json' \
  -d '{"label":"Mon provider","api_base":"https://api.exemple.com/v1","api_key":"…"}'
```

### 5.3 Dire qui sert quoi : les TMB Settings

Console → *Settings* : importez le fichier livré, ou composez le vôtre.
Un axe = une ligne `{ "key": "python", "label": "Python / scripts", "model": "<id du registre>" }`.
Le **registre** (`models`) décrit chaque modèle : son nom, son provider et son
id natif — `"or": "moonshotai/kimi-k3"` pour OpenRouter, `"endpoint": …` pour un
provider local ou déclaré. L'onglet *Routing table* montre, axe par axe, quel
modèle répond et par quel provider ; *Army* liste la flotte résolue.

> Le fichier livré est **100 % cloud**. Un profil 100 % local se compose à la
> main : déclarez votre moteur (5.1), enregistrez vos modèles dans le registre,
> affectez-les aux axes. Un réglage écrit à la main n'a besoin de rien d'autre
> — ni de nos tables, ni d'un serveur à nous (c'est testé).

## 6. Utiliser depuis vos outils

Une adresse, un token, un modèle.

```bash
export OPENAI_BASE_URL=http://<hôte>:4600/v1
export OPENAI_API_KEY=ck_…
```

| Outil | Réglage |
|---|---|
| SDK OpenAI, Aider, Continue, Cline… | `base_url` = `…:4600/v1`, `model` = `CoeOS` |
| Claude Code, SDK Anthropic | `ANTHROPIC_BASE_URL=http://<hôte>:4600`, clé = votre `ck_` — `/v1/messages` et `count_tokens` sont traduits |
| curl | voir ci-dessous |

```bash
curl "$OPENAI_BASE_URL/chat/completions" -H "authorization: Bearer $OPENAI_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"CoeOS","messages":[{"role":"user","content":"Relis ce contrat au regard du RGPD…"}]}'
```

**Imposer l'axe** (plus rapide, plus sûr quand l'agent connaît l'étape) :
`-H 'x-coeos-axis: legal_rgpd'`.

**Savoir qui a répondu** : lisez les en-têtes `x-coeos-axis`, `x-coeos-model`,
`x-coeos-provider` de la réponse ; l'onglet *Logs* de la console les garde.

**Raisonnement** : passez `enable_thinking` (ou `thinking` / `reasoning` selon
votre SDK) — CoeOS traduit vers le nom que chaque provider comprend. Si un
modèle refuse le no-think (« reasoning is mandatory »), CoeOS rejoue avec le
raisonnement activé plutôt que d'échouer.

## 7. Ce que CoeOS vous garantit (et prouve)

`tests/test_sovereignty.py` — 9 tests qui échouent si l'un de ces points cède :

- aucune notion de licence, d'expiration ou d'activation dans le code ;
- une box coupée de nous **route quand même** ;
- les seules adresses sortantes sont **vos** providers et, optionnellement, la
  réception de settings — désactivable (`COEOS_UPDATES_DISABLED`) ;
- un serveur de settings injoignable est un non-événement ;
- la consommation est comptée **pour vous**, jamais remontée ;
- ce qui sort de la box ne contient **rien** de vos données ;
- vous pouvez réaffecter chaque axe sans nos tables, et un setting écrit à la
  main n'a besoin de rien de nous.

## 8. Dépannage

| Symptôme | Cause probable | Remède |
|---|---|---|
| `503` sur tous les axes | le modèle d'un axe pointe vers un provider sans adresse ou sans clé | *Routing table* montre l'axe en défaut ; renseignez `api_base`/clé, ou réaffectez l'axe |
| `429` sur un axe | palier gratuit d'un modèle cloud | réaffectez l'axe à un modèle payant ou local |
| réponse vide avec un modèle local à raisonnement | le no-think a été envoyé sous un mauvais nom | CoeOS envoie `enable_thinking` à `odyssai` ; vérifiez que le provider est bien `odyssai`/déclaré avec `thinking_field` correct |
| `401` après avoir créé une clé | l'instance s'est verrouillée (comportement voulu) | utilisez le token `ck_` ; la première clé est admin |
| la console ne montre aucun modèle local | aucun `api_base` pour `odyssai` | §5.1 |

## 9. Surface d'API (référence rapide)

- Inference : `POST /v1/chat/completions`, `POST /v1/messages`,
  `POST /v1/messages/count_tokens`, `GET /v1/models`, `GET /v1/me`, `GET /v1/usage`.
- Découverte : `GET /.well-known/coeos.json` (profil + version),
  `GET /.well-known/inference-engine.json` (contrat de capacités), `GET /endpoints`.
- Administration (token admin) : `/admin/platform/providers[/{pid}]` (déclarer,
  adresser, clés), `/admin/providers/{pid}/test`, `/admin/settings*` (importer,
  charger, publier), `/admin/mapping[/axis/{axe}]`, `/admin/keys*`, `/admin/usage`,
  `/admin/army`, `/admin/coeos/decisions`.
