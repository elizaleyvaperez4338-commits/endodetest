# Usa una imagen base con Python
FROM python:3.13.2

# Define el directorio de trabajo
WORKDIR /app

# Actualiza los paquetes e instala FFmpeg
RUN apt update && apt upgrade -y && \
    apt install -y ffmpeg && \
    apt install -y build-essential &&\
    apt clean && \
    rm -rf /var/lib/apt/lists/*

# Copia los archivos del proyecto, excluyendo los ignorados en .dockerignore
COPY . /app

# Asegurar que el directorio de trabajo tiene los permisos correctos
RUN chmod -R 777 /app

# Actualiza pip
RUN pip install --upgrade pip

# Instala las dependencias
RUN pip install -r requirements.txt

# Exponer el puerto en el que corre la app
EXPOSE 7860

# Comando para ejecutar la aplicación
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
