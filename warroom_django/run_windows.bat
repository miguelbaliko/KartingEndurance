@echo off
cd /d "%~dp0"
echo Installing dependencies...
pip install -r requirements.txt
echo Migrating database...
python manage.py migrate
echo Setting up race session...
python manage.py setup_race
echo Creating admin user (skip if already exists)...
python manage.py createsuperuser --noinput --username admin --email admin@apxgp.com 2>nul || echo Admin already exists
echo.
echo Starting APX GP War Room on http://127.0.0.1:8080/
echo Command Center:  http://127.0.0.1:8080/
echo Strategist:      http://127.0.0.1:8080/strategist/
echo Admin:           http://127.0.0.1:8080/admin/
echo.
python manage.py runserver 8080
pause
