import os

import uvicorn

uvicorn.run("llm_gateway.app:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8010")))
