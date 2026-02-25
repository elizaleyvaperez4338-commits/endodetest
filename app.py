from fastapi import FastAPI
from contextlib import asynccontextmanager
import threading
import os

def run_bot():
    print('Bot lanzado desde FastAPI')
    os.system('python bot.py')

@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    yield  

app = FastAPI(lifespan=lifespan)

@app.get("/")
def read_root():
    return {"message": "Bot online"}
