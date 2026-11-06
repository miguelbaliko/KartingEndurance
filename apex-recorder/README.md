# Apex recorder (Alcanede 26H)

Always-on recorder for live.apex-timing.com/kartalcanede. Stores every frame, every lap with sectors,
pit events, race control messages and the track's archived sessions in Postgres.
Deployed on Render from `apex-recorder/render.yaml` (free web service + free Postgres).

Endpoints: `/` status, `/report`, `/laps.csv`, `/pits.csv`, `/events.csv`, `/archive_rows.csv`.
Raw frames in the `raw` table can be replayed into the war room with `warroom/apex_dump.py --replay`.
