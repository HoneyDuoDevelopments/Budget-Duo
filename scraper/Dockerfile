FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install only Chromium (skip Firefox/WebKit to save ~800MB)
RUN playwright install chromium

COPY . .

# The scraper doesn't run a persistent server — it's triggered by the backend via HTTP
# We run a tiny FastAPI to receive scrape commands
CMD ["python", "-m", "uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8501"]