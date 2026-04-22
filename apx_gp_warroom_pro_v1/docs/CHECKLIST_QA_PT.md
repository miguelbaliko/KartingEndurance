# Checklist QA — antes de entregar ao APX GP

## Ambiente
- [ ] Python 3.10+ instalado
- [ ] `python app.py` arranca sem erro
- [ ] `http://127.0.0.1:8080/` abre
- [ ] `http://127.0.0.1:8080/strategist` abre

## Rede local
- [ ] descobrir IP do laptop servidor
- [ ] outro PC abre `http://IP:8080/`
- [ ] outro PC actualiza dados

## Dados
- [ ] modo `MANUAL` lê `data/manual_input.csv`
- [ ] alteração no CSV aparece no dashboard
- [ ] POST `/api/lap` adiciona lap
- [ ] POST `/api/manual` muda piloto/kart/box

## Estratégia
- [ ] stint > 40 min gera PREPARE
- [ ] stint > 44 min gera BOX
- [ ] pace_drop > 0.30 gera PREPARE
- [ ] pace_drop > 0.50 gera BOX
- [ ] kart GOOD + fechar gap gera ATTACK
- [ ] últimos 5 min geram CRITICAL / NO PIT WINDOW

## Apex
- [ ] URL real Alcanede no `config.json`
- [ ] `mode` = LIVE_SCRAPE
- [ ] parser devolve TeamTiming com posição/equipa/lap/pits
- [ ] fallback manual funciona se Apex falhar

## Câmaras
- [ ] BOX ENTRY visível
- [ ] PIT QUEUE ou KEY CORNER visível
- [ ] se câmara falhar, dashboard continua funcional

## Dia de corrida
- [ ] carregadores ligados
- [ ] power backup
- [ ] router testado
- [ ] IP do servidor escrito no papel
- [ ] manual CSV pronto como plano B
