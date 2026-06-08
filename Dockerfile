# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Dépendances (fpdf2 optionnel — inclus pour que TICKET_FORMAT=pdf fonctionne)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code source
COPY epos_web.py .

# Le .env est monté via docker-compose (volume), pas copié dans l'image
# pour éviter d'y inclure des valeurs sensibles.

EXPOSE 80
EXPOSE 8080

CMD ["python", "epos_web.py"]
