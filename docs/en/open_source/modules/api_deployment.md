# MemOS API

## Default entry and deployment

- Use **`server_api.py`** as the API service entry for **public open-source usage**.
- You can deploy via **`docker/Dockerfile`**.

The above is the default, general way to run and deploy the API.

## Single entry point and compatibility alias

- **`server_api.py`** is the single API entry point; it already includes authentication (`AUTH_ENABLED`), rate limiting (`RATE_LIMIT_ENABLED`), security headers and CORS. Both **`docker/Dockerfile`** and **`docker/Dockerfile.krolik`** use this entry point.
- **`server_api_ext.py`** is kept only as a deprecated import-compatibility alias (it forwards `memos.api.server_api:app`) and must not be used as a deployment entry point.
