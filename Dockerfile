FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    STOCK_AGENT_HOME=/data PORT=8080 TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt numpy \
 && useradd -r -u 10001 -m -d /home/agent agent \
 && mkdir -p /data && chown agent:agent /data
COPY --chown=root:root . .

USER agent
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python", "-m", "stock_agent", "serve"]
