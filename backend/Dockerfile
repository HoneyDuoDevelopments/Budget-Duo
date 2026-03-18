FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create static dir and copy frontend if it exists at build time
RUN mkdir -p /app/static

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8500"]
