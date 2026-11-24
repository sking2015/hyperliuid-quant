FROM python:3.12-slim
WORKDIR /srv/backend
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY app app
COPY migrations migrations
ENV PYTHONUNBUFFERED=1 DATA_DIR=/var/lib/quant
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

