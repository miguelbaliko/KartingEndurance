# Especificação Técnica — APX GP War Room

## Objetivo final
Sistema de apoio à decisão para corrida endurance karting de 13h com 23 paragens obrigatórias, stint máximo de 45 minutos, 3 minutos mínimos de box e troca obrigatória de kart.

## Stack
- Python 3.10+
- Standard library no MVP
- Opcional para scraping avançado: requests, BeautifulSoup, Selenium
- Frontend: HTML/CSS/JS simples
- Servidor local: http.server/threading

## Endpoints internos

### GET /api/state
Devolve estado completo:
- metrics
- decision
- top_table
- laps
- events
- cameras

### POST /api/manual
JSON:
```json
{
  "current_driver": "Emil",
  "current_kart": "GOOD",
  "box_status": "EMPTY",
  "reset_stint": true
}
```

### POST /api/lap
JSON:
```json
{
  "team": "APX GP",
  "lap_time": "46.512",
  "lap": 123
}
```

### GET /api/health
Estado básico.

## Dashboard

### Command View `/`
Para team manager.
- APX logo
- posição
- piloto
- stint
- avg5
- trend
- kart rating
- status grande
- comandos
- câmaras
- top table
- lap trace

### Strategist View `/strategist`
Para operador.
- input manual
- logs
- top table
- câmaras

## Adaptação Apex
Ficheiro: `apx/apex_client.py`.

O método que deve devolver dados é:

```python
ApexClient.fetch() -> list[TeamTiming]
```

A implementação actual:
1. lê HTML
2. tenta parse de tabelas
3. tenta JSON embutido
4. se falhar, devolve lista vazia

O programador deve substituir/adicionar a fonte exacta vista no DevTools da página Alcanede.

## Critérios de aceitação
- Dashboard abre em 2 PCs no mesmo WiFi
- `/api/state` actualiza a cada 5–10s
- modo manual funciona mesmo sem internet
- se Apex falha, dashboard não cai
- status HOLD/PREPARE/BOX/ATTACK aparece correctamente
- stint clock e reset funcionam
- câmaras aparecem no painel direito quando URLs são válidas
