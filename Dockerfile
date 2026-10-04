# Imagem minima: o Ponto e um arquivo Python sem dependencia.
FROM python:3.12-alpine
# tzdata: sem ele a semana vira na segunda 00:00 UTC, nao na sua
RUN apk add --no-cache tzdata
WORKDIR /ponto
COPY servidor ./servidor
COPY web ./web
ENV PONTO_BANCO=/dados/ponto.db \
    PONTO_PORTA=8095 \
    PONTO_HOST=0.0.0.0 \
    TZ=America/Sao_Paulo \
    PYTHONUNBUFFERED=1
VOLUME /dados
EXPOSE 8095
CMD ["python3", "servidor/ponto.py"]
