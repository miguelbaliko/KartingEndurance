#!/bin/bash
echo "Installing dependencies..."
pip install -r requirements.txt --quiet
echo ""
echo "Starting APX GP War Room..."
echo "Open http://localhost:8080 in your browser"
echo ""
python app.py
