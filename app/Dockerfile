FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 8080

ENV LOGIN_PASSWORD=vending123
ENV SECRET_KEY=production-secret-key

CMD ["python", "app.py"]
