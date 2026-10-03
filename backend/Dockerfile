# Python 3.11 is the interpreter the test suite and broker SDKs were verified on.
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --create-home --uid 10001 iort \
 && mkdir -p /app/data && chown -R iort:iort /app
COPY --chown=iort:iort backend/app ./app
COPY --chown=iort:iort frontend ./frontend
USER iort
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"
# One worker only: the kill switch, risk monitor and per-broker order locks live in this process.
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--workers","1"]
