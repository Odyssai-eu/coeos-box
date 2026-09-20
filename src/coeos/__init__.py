"""CoeOS — passerelle BYOK auto-hébergée, compatible OpenAI et Anthropic.

Classe chaque requête sur un axe de compétence (la « TMB Settings ») et la
relaie au modèle prouvé meilleur sur cet axe, via les clés du client. La box
tourne chez le client : les clés et les données ne remontent jamais.

Cœur de routage extrait d'OdyssAI-X (RFC #63), re-hébergé
ici comme socle de la box BYOK.
"""

__version__ = "0.23.0"
