FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 GAPP_DB=/data/gapp.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gapp ./gapp
COPY sources.yaml .
RUN useradd -u 10001 -m gapp && mkdir /data && chown gapp /data
USER gapp
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "gapp.web:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
