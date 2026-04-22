# APX GP War Room v1 — Dashboard de Estratégia Endurance Karting

## 1. Objetivo
Construir/usar um sistema local em Python que transforma dados de Live Timing (Apex Timing) + inputs manuais em decisões claras de corrida:

- `HOLD` — manter na pista
- `PREPARE` — preparar paragem
- `BOX` — entrar na box
- `ATTACK` — stint de ataque controlado
- `CRITICAL` — situação crítica / janela fechada

Este pacote já inclui um MVP funcional com dashboard profissional APX GP, modo manual, API local, lógica estratégica e estrutura para adaptar o scraper Apex.

---

## 2. Contexto da corrida
Regulamento relevante:

- Corrida: 13 horas = 780 minutos
- Paragens obrigatórias: 23
- Stint máximo por piloto: 45 minutos
- Paragem de box obrigatória: 3 minutos
- Troca de kart obrigatória em cada paragem
- Últimos 5 minutos: não pode parar
- Sistema de cronometragem: Apex Timing

O software deve respeitar esta lógica. A estratégia é ganhar tempo com timing, consistência e controlo de risco.

---

## 3. Como correr o sistema

### Windows
1. Instalar Python 3.10+.
2. Abrir a pasta do projeto.
3. Duplo clique em:

```bat
run_windows.bat
```

Ou na consola:

```bash
python app.py
```

### Mac/Linux

```bash
./run_mac_linux.sh
```

Dashboard principal:

```text
http://127.0.0.1:8080/
```

Dashboard do estratega:

```text
http://127.0.0.1:8080/strategist
```

Noutro PC da mesma rede WiFi:

```text
http://IP_DO_LAPTOP_SERVIDOR:8080/
```

---

## 4. Arquitetura

```text
app.py                  servidor HTTP + worker de dados
apx/apex_client.py      leitor Apex Timing, a adaptar à página real
apx/manual_client.py    fallback CSV
apx/strategy_engine.py  lógica de corrida
apx/state_store.py      memória + CSV/JSON local
static/                 CSS/JS/logo
html/templates          dashboards
data/                   CSV input/output/logs
```

Sem dependências obrigatórias. Corre com standard library Python.

---

## 5. Configuração
Editar `config.json`.

Campos importantes:

```json
{
  "team_name": "APX GP",
  "event_url": "https://live.apex-timing.com/ALCANEDE_URL_REAL/",
  "mode": "MANUAL",
  "refresh_interval_seconds": 5,
  "port": 8080
}
```

### Modo manual

```json
"mode": "MANUAL"
```

Usa `data/manual_input.csv`.

### Modo live scrape

```json
"mode": "LIVE_SCRAPE"
```

Tenta ler a página Apex. Se não encontrar dados, cai automaticamente para CSV manual.

---

## 6. O que o programador deve fazer primeiro

### Passo 1 — Validar MVP
- Correr `python app.py`
- Abrir `/`
- Confirmar dashboard
- Editar `data/manual_input.csv`
- Confirmar que o dashboard actualiza

### Passo 2 — Adaptar Apex real
No dia em que existir a URL Alcanede:

1. Abrir a página no Chrome.
2. DevTools → Network.
3. Filtrar por XHR / Fetch / WS.
4. Procurar endpoint que devolve:
   - posição
   - equipa
   - última volta
   - melhor volta
   - voltas
   - pits
   - gap
   - interval
   - delta
5. Implementar esse endpoint em `apx/apex_client.py`.
6. Converter cada linha para `TeamTiming`.

`TeamTiming` esperado:

```python
TeamTiming(
    position=1,
    name="APX GP",
    last_lap=46.512,
    best_lap=46.300,
    laps=124,
    pits=5,
    gap="+3.2",
    interval="+1.1",
    delta="-0.2",
    on_track="34:12"
)
```

---

## 7. Lógica estratégica já implementada

### Stint
- `>= 40 min` → PREPARE
- `>= 42 min` → PREPARE forte
- `>= 44 min` → BOX FORCED

### Pace
- pace drop `> 0.30s` → PREPARE
- pace drop `> 0.50s` → BOX

### Pit plan
- calcula paragens esperadas:

```python
expected_pits = (race_elapsed / 780min) * 23
```

Se a equipa estiver atrás do plano → PREPARE.

### Undercut
Possível quando:
- stint entre 24 e 40 minutos
- kart/pace está a perder > 0.20s/lap
- ainda não é stop forçado

---

## 8. Câmaras
Editar `config.json`:

```json
"cameras": [
  {"name": "BOX ENTRY", "url": "http://192.168.1.50:8080/video"},
  {"name": "PIT QUEUE", "url": "http://192.168.1.51:8080/video"}
]
```

O dashboard só faz embed. Não processa vídeo.

---

## 9. Operação em 2 ou 3 PCs

### Laptop 1 — servidor
- corre `python app.py`
- ninguém mexe durante a corrida

### Laptop 2 — estratega
- abre `/strategist`
- controla manual state
- valida Apex Timing
- vê eventos/logs

### Laptop 3 — team manager
- abre `/`
- vê dashboard principal
- toma decisões

---

## 10. Entrega esperada pelo programador

Prioridade absoluta:
1. Scraper Apex real funcional
2. Dashboard estável em rede local
3. Inputs manuais funcionais
4. Streams de câmaras visíveis
5. Logs e fallback se Apex falhar

Não fazer overengineering. No dia da corrida, estabilidade vale mais do que elegância técnica.
