from logging import WARNING
import os


API_ID = os.environ.get("API_ID","28193212")
API_HASH = os.environ.get( "API_HASH","14c5ec97b18a391d526e4a461e4a5f82")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8624645865:AAER4yf2hnxA-47311L5IQ5zlhgHz8pY-mA") 
ADMINS_IDS = [int(x) for x in os.environ.get("ADMINS", "5644237743").split(",") if x]
USERS = [int(x) for x in os.environ.get("USERS", "").split(",") if x]
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://carlosganz368_db_user:Carlos060706@cluster0.ija78j3.mongodb.net")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "compress2")
BOT_IS_PUBLIC = os.environ.get("BOT_IS_PUBLIC", "false").lower() == "true"
