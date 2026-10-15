# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    API_BASE=http://127.0.0.1:8000 \
    PORT=8501

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app --home /app app
COPY --chown=app:app requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app app ./app
COPY --chown=app:app web ./web
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app samples ./samples
COPY --chown=app:app eval ./eval
COPY --chown=app:app .streamlit ./.streamlit

RUN chmod +x scripts/start_railway.sh && mkdir -p data/uploads outputs && chown -R app:app data outputs
USER app

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8501')+'/_stcore/health',timeout=5)"

CMD ["./scripts/start_railway.sh"]
