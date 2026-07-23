FROM python:3.12-slim

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код пробрасывается через volume для удобства разработки
# COPY app/ /usr/src/app/app/

CMD ["python", "-u", "app/listener.py"]
