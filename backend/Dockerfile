FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt  && useradd --create-home --uid 10001 iort  && mkdir -p /app/data  && chown -R iort:iort /app
COPY backend/app ./app
COPY frontend ./frontend
COPY config ./config
RUN chown -R iort:iort /app
USER iort
EXPOSE 8000
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
