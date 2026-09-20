# coeos-box — la box de CoeOS

**CoeOS est le client intelligent d'OdyssAI — un système d'exploitation IA
complet, avec le routeur intelligent inclus.** Vos outils lui parlent comme à
un seul modèle (API OpenAI `/v1/chat/completions` **et** Anthropic
`/v1/messages`) ; il classe chaque requête sur un **axe de compétence**
(Python, debug, juridique RGPD, rédaction, planification…) et l'envoie au
**modèle prouvé meilleur sur cet axe** — avec **vos** clés, sur **votre**
machine. Les modèles peuvent être locaux (un moteur
[OdyssAI-X](https://github.com/Odyssai-eu/OdyssAI-X) sur votre LAN) ou cloud
(OpenRouter, ou tout provider OpenAI-compatible que vous déclarez). **Ce repo
est la box** — le routeur, les comptes, les clés, les quotas et la console
**CoeOS** ; le client est **Nemo**, [Odyssai-eu/coeos](https://github.com/Odyssai-eu/coeos),
une app macOS signée et notarisée qui parle à la box ou directement au moteur.

```
 vos outils ──── clé ck_ ────▶  coeos-box  ──── vos clés ────▶  OpenRouter / providers déclarés
 (Claude Code, Aider, Cline,      │ classe la requête        └▶  OdyssAI-X (local, sans clé)
  Continue, SDK OpenAI/Anthropic) │ sur un axe, relaie
                                   └── console CoeOS (:4600/dashboard)
```

- **Rien ne sort de chez vous** sauf la requête vers le provider que *vous* avez
  choisi. Pas de télémétrie, pas de kill-switch, pas d'appel obligatoire vers nous
  — c'est testé (`tests/test_sovereignty.py`, 9 invariants). Si vous cessez de
  nous payer, ça continue de tourner.
- **Une seule adresse pour toute la flotte** : vos agents appellent CoeOS ; le
  meilleur modèle par étape est choisi pour eux, d'après des benchmarks réels
  (les *TMB Settings*), pas d'après un classement générique.
- **Multi-utilisateurs** : comptes, tokens `ck_` par utilisateur, quotas
  (tokens/jour, requêtes/min), clés provider chiffrées au repos (Fernet).

Guide complet — installation, branchement d'un moteur local, axes, comptes,
garanties : [`doc/USER-GUIDE.md`](doc/USER-GUIDE.md).

## Démarrer

### Docker (recommandé)

```bash
docker compose up -d --build
curl -s http://localhost:4600/health
```

Console **CoeOS** sur `http://<hôte>:4600/dashboard` (aussi servie sur `:4800`).
Tant qu'aucune clé n'existe, l'instance est ouverte (usage local) ; **la première
clé créée devient admin** et verrouille `/v1/*` et l'administration. La config
(clés chiffrées, utilisateurs, settings) vit dans le volume `./data`, jamais
dans l'image.

### Python (≥ 3.10)

```bash
pip install .
coeos-box                 # --host, --port (4600), --console-port (4800)
```

## Brancher vos modèles

- **Cloud** : saisissez votre clé OpenRouter dans la console (onglet *Providers*),
  ou amorcez avec `OPENROUTER_API_KEY`. Tout autre provider OpenAI-compatible
  se déclare avec son `api_base` et sa clé.
- **Local (OdyssAI-X ou tout serveur OpenAI-compatible sur votre LAN)** : le
  provider `odyssai` existe d'origine, **sans clé** ; donnez-lui l'adresse de
  votre moteur :

```bash
curl -X PUT http://localhost:4600/admin/providers/odyssai \
  -H 'content-type: application/json' -d '{"api_base":"http://<votre-moteur>:8000/v1"}'
```

Puis, dans les *TMB Settings* (onglet *Settings*), affectez vos modèles locaux
aux axes où ils sont les meilleurs — un réglage écrit à la main n'a besoin de
rien d'autre.

## Utiliser

```bash
export OPENAI_BASE_URL=http://<hôte>:4600/v1
export OPENAI_API_KEY=ck_…            # le token émis pour vous
curl "$OPENAI_BASE_URL/chat/completions" -H "authorization: Bearer $OPENAI_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"CoeOS","messages":[{"role":"user","content":"Écris un script Python qui…"}]}'
```

La réponse indique ce qui a servi : en-têtes `x-coeos-axis`, `x-coeos-model`,
`x-coeos-provider`. Un agent qui connaît déjà la compétence d'une étape peut
l'imposer : `-H 'x-coeos-axis: legal_rgpd'`. Les clients Anthropic (Claude Code,
SDK) passent par `/v1/messages`, traduit à la volée.

## Configuration

Tout est optionnel — clés et adresses se saisissent dans la console.

| Variable | Rôle |
|---|---|
| `COEOS_CONFIG` | Chemin de la config (défaut `./coeos-config.json`, Docker `/data/coeos-config.json`) |
| `COEOS_HOST` / `COEOS_PORT` / `COEOS_CONSOLE_PORT` | Bind (défaut `0.0.0.0`, `4600`, `4800`) |
| `OPENROUTER_API_KEY` | Amorçage de la clé OpenRouter ; sinon console |
| `COEOS_API_KEY` | Clé admin d'amorçage ; ensuite tokens `ck_` par utilisateur |
| `COEOS_MASTER_KEY` | Clé maître du coffre (sinon fichier `coeos-master.key` en 0600) |
| `COEOS_DAY_TOKENS_DEFAULT` / `COEOS_RPM_DEFAULT` | Quotas par défaut d'un compte |
| `COEOS_MASTER_URL` / `COEOS_UPDATES_DISABLED` | Réception optionnelle de settings publiés ; désactivable, un master injoignable est sans effet |

## Comptes et clés

```bash
python -m coeos.accounts create-user alice --mode byok
python -m coeos.accounts issue-key alice --name laptop
python -m coeos.accounts list
python -m coeos.accounts revoke ck_1234
```

Un token n'est affiché qu'une fois (seul son sha256 est stocké). Chaque compte
est servi par le coffre — sa propre clé (`byok`) ou la clé plateforme selon son
mode, jamais la clé globale de l'opérateur en repli.

## Tests

```bash
python -m pytest tests -q
```

## OdyssAI — deux composants

| Composant | Repo | Rôle |
|---|---|---|
| **OdyssAI-X** (moteur) | [Odyssai-eu/OdyssAI-X](https://github.com/Odyssai-eu/OdyssAI-X) | inférence MLX distribuée / replica / VLM sur Apple Silicon ; API OpenAI + Anthropic ; dashboard. AGPL-3.0 |
| **CoeOS** (client intelligent) | [Odyssai-eu/coeos](https://github.com/Odyssai-eu/coeos) — **Nemo**, l'app (`.dmg` signé et notarisé) · la box est **ce repo** | système d'exploitation IA complet, routeur intelligent inclus : chaque requête va au modèle prouvé meilleur sur sa compétence — local sur le moteur, ou cloud avec vos clés. Nemo tourne sur votre Mac ; la box porte le routeur, les comptes, les tokens, les quotas et la console CoeOS. MIT |

Aussi dans l'organisation : [Guardian](https://github.com/Odyssai-eu/odyssai-guardian) (détection de contenu confidentiel avant tout départ vers un provider cloud, MIT), [odyssai-services](https://github.com/Odyssai-eu/odyssai-services) (bench et outillage), [mlx-swift-lm](https://github.com/Odyssai-eu/mlx-swift-lm) (MIT).

## Provenance et licence

Le cœur de routage (`coeos_resolve`) est né dans OdyssAI-X et est re-hébergé
ici comme socle de la box. Console : **CoeOS**.
**MIT** — voir [LICENSE](LICENSE).
