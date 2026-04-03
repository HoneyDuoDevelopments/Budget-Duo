FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# Install Xvfb for headed browser mode (Apple Card requires it)
RUN apt-get update && \
    apt-get install -y xvfb && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install only Chromium
RUN playwright install chromium

COPY . .

# Start Xvfb in background, then run the server
CMD xvfb-run --auto-servernum --server-args="-screen 0 1280x800x24" \
    python -m uvicorn app.server:app --host 0.0.0.0 --port 8501