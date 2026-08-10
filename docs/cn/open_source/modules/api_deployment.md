# MemOS API

## 默认入口与部署方式

- **公开开源使用**时，请使用 **`server_api.py`** 作为 API 服务的入口。
- 您可以通过 **`docker/Dockerfile`** 进行部署。

以上是运行和部署 API 的默认通用方式。

## 单一入口与兼容别名

- **`server_api.py`** 是唯一的 API 入口，已内置鉴权（`AUTH_ENABLED`）、限流（`RATE_LIMIT_ENABLED`）、安全响应头与 CORS 配置；**`docker/Dockerfile`** 与 **`docker/Dockerfile.krolik`** 均使用该入口。
- **`server_api_ext.py`** 仅保留为 deprecated 兼容导入别名（转发 `memos.api.server_api:app`），不作为部署入口。
