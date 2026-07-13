# instalamos python
FROM python:3.10-slim

# Creamos la app
WORKDIR /app

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements y despues lo instalamos
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código
COPY . .

# Puerto para Hugging Face
EXPOSE 7860

# Ejecutar bot
CMD ["python", "bot.py"]