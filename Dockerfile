# Minimal text/API prototype. No models, host tools, private sources or credentials.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 NAVIGATOR_BIND_ADDRESS=0.0.0.0 NAVIGATOR_WORKER=0 \
    NAVIGATOR_DB_PATH=/var/lib/navigator/module.sqlite \
    NAVIGATOR_AUTH_PATH=/run/secrets/navigator/access.json
WORKDIR /app
RUN groupadd --gid 10001 navigator && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin navigator \
    && mkdir -p /var/lib/navigator && chmod 700 /var/lib/navigator && chown navigator:navigator /var/lib/navigator
COPY --chown=10001:10001 navigator/ ./navigator/
USER navigator:navigator
EXPOSE 8080
# Administrator must include 127.0.0.1:${PORT} in NAVIGATOR_ALLOWED_HOSTS for this probe.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c 'import json,os,urllib.request; opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); r=opener.open("http://127.0.0.1:"+os.environ["PORT"]+"/healthz",timeout=2); assert r.status==200 and json.load(r)=={"status":"ok"}'
ENTRYPOINT ["python", "-m", "navigator", "serve"]
