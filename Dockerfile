FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Asılılıqları quraşdırırıq
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bütün layihə fayllarını kopyalayırıq
COPY . .

# Botu işə salırıq
CMD ["python", "bot.py"]
