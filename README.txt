Положите сюда server_ca.crt — корневой сертификат вашего сервера
(для Caddy с "tls internal": caddy_data/caddy/pki/authorities/local/root.crt).
Если файл есть, приложение доверяет ТОЛЬКО ему (защита от подмены сертификата).
Если используете Tailscale-сертификат или Let's Encrypt — файл не нужен.
