# Playwright 公式イメージ（Chromium と依存ライブラリ入り）。
# タグの Playwright バージョンは requirements.txt で入る playwright と合わせること。
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV TZ=Asia/Tokyo \
    PYTHONUNBUFFERED=1 \
    F1HOTEL_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
VOLUME ["/data"]

# 内蔵スケジューラで常駐（9:00/21:00 JST ＋ 密度アップ期間は毎時）
CMD ["python", "monitor.py", "schedule", "--run-now"]
