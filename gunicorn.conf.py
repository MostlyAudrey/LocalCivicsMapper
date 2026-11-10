# gunicorn -c gunicorn.conf.py index:app
# The web app is light (DB reads + JSON); pipeline work runs in the separate civic-worker service.
bind = "0.0.0.0:8080"
workers = 3
threads = 4
worker_class = "gthread"
timeout = 60
graceful_timeout = 20
accesslog = "-"
errorlog = "-"
loglevel = "info"
proxy_allow_ips = "127.0.0.1"
