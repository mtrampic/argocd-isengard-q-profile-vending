FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

ENV ADMIN_PASSWORD=admin123
ENV SECRET_KEY=production-secret-key

CMD ["python", "app.py"]
