import os
import uvicorn
from app.main import app

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.getenv("PORT", "5000")),
                reload=False, proxy_headers=False, workers=1)
