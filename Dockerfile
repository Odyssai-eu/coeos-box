FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# La config (clés BYOK une fois saisies au dashboard, users, setting) vit sur le
# volume — jamais dans l'image.
ENV COEOS_CONFIG=/data/coeos-config.json \
    COEOS_PORT=4600
VOLUME /data
EXPOSE 4600 4800

# La box : routing + vault + users, chez le client.
CMD ["coeos-box"]
