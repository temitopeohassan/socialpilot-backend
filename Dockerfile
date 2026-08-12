FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for Docker layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code and set up storage directories
COPY . .
RUN mkdir -p /app/data /app/uploads

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]