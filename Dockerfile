FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
COPY app.py .
COPY config.py .
COPY api api
COPY core core
COPY handlers handlers
COPY services services
COPY storage storage
COPY assets/bells.png assets/bells.png

CMD ["python", "bot.py"]
