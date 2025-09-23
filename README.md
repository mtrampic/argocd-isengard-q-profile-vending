# Q Profile Vending Service

A secure Flask web application for Q profile management, deployed on Kubernetes via ArgoCD GitOps.

## Features

- Simple password authentication
- Responsive web interface
- Health check endpoint
- Kubernetes-ready deployment
- GitOps deployment via ArgoCD

## Access

- **URL**: https://q-profile-vending.trampic.info
- **Password**: `RaiffeisenInformatik2025!`

## Architecture

- **Frontend**: Flask with HTML templates
- **Backend**: Python Flask
- **Database**: None (stateless)
- **Deployment**: Kubernetes on ARM64 Raspberry Pi cluster
- **CI/CD**: ArgoCD GitOps
- **Ingress**: Cloudflare Tunnel

## Local Development

```bash
cd app
pip install -r requirements.txt
python app.py
```

## Deployment

1. Push changes to GitHub
2. ArgoCD automatically syncs and deploys
3. Service available via Cloudflare tunnel

## Environment Variables

- `ADMIN_PASSWORD`: Login password (default: admin123)
- `SECRET_KEY`: Flask session secret key
