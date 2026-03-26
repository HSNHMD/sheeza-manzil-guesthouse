web: FLASK_APP=run.py flask db upgrade && gunicorn run:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT --access-logfile - --log-level info
