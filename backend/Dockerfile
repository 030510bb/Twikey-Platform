# Production image for the Twikey Sales Platform email backend.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render (and most PaaS platforms) inject the port to listen on via $PORT.
# Default to 8000 so this also works fine for plain "docker run" locally.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
