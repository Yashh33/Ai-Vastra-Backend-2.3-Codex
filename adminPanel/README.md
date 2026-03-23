# Ai Vastra Admin Panel

Private web admin for onboarding and managing shops.

## Features (V1)

- Admin-secret login (`x-admin-secret`)
- Create shop login (email + password)
- Add opening credits while creating shop
- Edit shop name + carousel default
- Reset shared shop password
- Upload shop logo
- Create hero folders for a shop
- Upload hero images to selected folder

## Environment

Create `.env` (or copy from `.env.example`):

```bash
VITE_ADMIN_API_BASE_URL=http://localhost:8000
```

## Run locally

```bash
npm install
npm run dev -- --host 0.0.0.0 --port 5174
```

## Build

```bash
npm run build
```
