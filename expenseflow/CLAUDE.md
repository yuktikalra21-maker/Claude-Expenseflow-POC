# ExpenseFlow API
 
## What this is
A small expense submission and approval API. PoC, not production.
One user journey: submit an expense, convert it to a base currency, approve or reject it.
 
## Stack
- Python 3.12, FastAPI, Uvicorn
- SQLAlchemy ORM on SQLite (file: expenseflow.db) for the PoC
- httpx for the external FX rate call
- pydantic v2 for request and response models
- pytest for tests
 
## Conventions
- Layout: app/main.py, app/db.py, app/models.py, app/schemas.py, app/routes.py
- Type hints on every function. Docstrings on every endpoint.
- Money is stored as integer minor units (paise / cents), never float.
- Base currency is INR. All amounts are normalised to base on write.
- Never hardcode secrets. Read them from environment variables via python-dotenv.
 
## Run and test
- Run:  python -m uvicorn app.main:app --reload
- Test: python -m pytest -q
 
## Do not touch
- Do not edit .venv, .git, or expenseflow.db directly.
- Do not add new third-party dependencies without telling me first.
- Do not invent endpoints that are not in the brief.
