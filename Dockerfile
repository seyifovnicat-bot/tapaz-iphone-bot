FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Bakı vaxt qurşağını sistem səviyyəsində təyin edirik
ENV TZ="Asia/Baku"
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# Asılılıqları quraşdırırıq
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

# Bütün layihə fayllarını kopyalayırıq
COPY . .

# Botu işə salırıq
CMD ["python", "bot.py"]
