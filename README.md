# API Gateway Service

Central entry point for all API requests. Handles authentication, rate limiting, and request routing to backend services.

## Features

- Request routing to backend services
- JWT authentication and authorization
- Rate limiting
- API versioning (/api/v1/)
- Request/response logging

## Development

```bash
cd backend/api-gateway
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn src.main:app --reload --port 8000
```

