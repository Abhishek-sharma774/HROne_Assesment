# Employee Attendance & Analytics API

Simple FastAPI + MongoDB project (HROne trainee assignment).

## Quick start (for anyone who clones this repo)

**Step 1 — set up the database.** Use this command to set up the DB:

```bash
docker compose up -d
```

That is all the DB setup needed. It starts MongoDB 7 on `localhost:27017`
with saved data (named volume), ready for the API below.

## Setup (details)

### 1. Database setup (MongoDB 7 via Docker)

```bash
docker compose up -d
```

This starts MongoDB on `localhost:27017` with saved data (named volume).
Check it is up:

```bash
docker ps --filter name=hrone-mongo-dev
```

If port `27017` is already used by another container, stop that one first
(`docker stop <name>`). To stop this DB: `docker compose down`.
To delete the saved data as well: `docker compose down -v`.

No password or secret is needed for local development. The file
`.env.example` shows which variables the app reads:

```bash
cp .env.example .env   # already contains working local defaults
```

### 2. Backend setup (FastAPI)

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000 --reload
```

- API: `http://localhost:8000`
- Interactive docs: `http://localhost:8000/docs`
- Health check: `GET /health` (200 when MongoDB answers, otherwise 503)

All backend code lives in `app/main.py` only.

### 3. Frontend setup (demo HRMS UI, optional)

No build step is needed. While the API is running, open
`hrms-ui-react.html` in a browser (double-click the file).

It is a single-file React UI with 4 tabs — Dashboard, Employees,
Attendance, Reports — that calls the local API. It is a local demo
only and is NOT part of the assessment submission.

## What is implemented
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`
- `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

Business rules R1-R10 follow `openapi.yaml`.

## Notes
- Everything is complete, nothing is missing.
- On the first start, index creation can take a few seconds.
- Never commit `.env`, secrets, or data dumps.
