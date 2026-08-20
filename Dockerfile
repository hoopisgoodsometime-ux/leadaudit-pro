FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5001

ENV PORT=5001
ENV FLASK_APP=app.py

CMD ["python", "app.py"]
