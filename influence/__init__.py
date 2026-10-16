"""AI 인플루언서 엔진 — 공신력 있는 시장 목소리를 모아 AI가 매매를 판단한다.

구성:
  roster     — 공신력 소스 로스터(애널리스트·내부자·미디어·SNS·군중) + 가중치
  providers  — 소스별 증거(Evidence) 수집기
  engine     — 수집 → AI 판단(Verdict) → 0~100 점수 (자동매매 파이프라인에 주입)
"""
from influence import roster, providers, engine  # noqa: F401
