FROM python:3.11-slim

WORKDIR /app

# Zeitzone setzen (wichtig für die Fälligkeits-Checks!)
ENV TZ=Europe/Zurich
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Verzeichnisse für Persistenz erstellen
RUN mkdir -p /app/data/uploads

CMD ["python", "app.py"]