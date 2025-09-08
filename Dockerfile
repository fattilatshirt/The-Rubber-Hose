# Usa Python 3.11 come base
FROM python:3.11-slim

# Imposta la cartella di lavoro
WORKDIR /app

# Copia i file di progetto nel container
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Avvia il bot
CMD ["python", "main.py"]
