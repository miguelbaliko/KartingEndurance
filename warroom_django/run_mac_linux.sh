#!/bin/bash
set -e
cd "$(dirname "$0")"
pip install -r requirements.txt
python manage.py migrate
python manage.py setup_race
python manage.py createsuperuser --noinput --username admin --email admin@apxgp.com 2>/dev/null || true
echo ""
echo "Starting APX GP War Room on http://127.0.0.1:8080/"
python manage.py runserver 8080
