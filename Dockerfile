# ⭕️ 1行目にPython 3.12を物理的に指定（firebase-adminが安定して動くバージョンです）
FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app