# AI 트레이딩 서버 이미지 — 파이썬 의존성만 담는다.
# 코드와 장부(상태 파일)는 이미지에 넣지 않고 서버의 /opt/trade 를 /app 으로 마운트한다:
#   코드 갱신 = deploy.sh 가 파일만 바꾸고 재시작, 장부 = 컨테이너를 지워도 그대로 남음.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Seoul

# tzdata: 일부 기록(주문 시각 등)이 로컬 시각(KST) 기준이라 컨테이너 시간대를 맥과 맞춘다.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /tmp/requirements.lock
RUN pip install -r /tmp/requirements.lock

WORKDIR /app
CMD ["python", "-m", "ui_ng.dashboard"]
