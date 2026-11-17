from fastapi import FastAPI

app = FastAPI(title="Lead Radar")


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
