# Используем официальный Python 3.12 образ
FROM python:3.12-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем переменные окружения
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 1. СНАЧАЛА копируем requirements (Команда Docker)
COPY requirements.txt .

# 2. ПОТОМ запускаем установку (Команды Linux объединяем через &&)
RUN apt-get update && apt-get install -y gcc python3-dev build-essential \
    && pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt \
    && apt-get purge -y gcc python3-dev build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Копируем остальные файлы проекта
COPY . .

# Создаем директорию для данных
RUN mkdir -p /app/data

# Открываем порт для health сервера
EXPOSE 8080

# Запускаем бота
CMD ["python", "src/bot.py"]
